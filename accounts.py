"""Customer accounts (Flask blueprint): sign up, log in, order history, profile,
saved address, password reset and email verification.

Guest checkout still works — an account is optional. Registered from app.py via init(core).
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

core = None
bp = Blueprint("acct", __name__, url_prefix="/account")
DUMMY_HASH = generate_password_hash("not-a-real-password")     # used to equalise login timing
COMMON_PASSWORDS = {"password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123",
                    "iloveyou", "11111111", "abcd1234", "admin123", "welcome1", "letmein1", "qwertyuiop"}


def init(c):
    global core
    core = c
    c.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)     # "keep me signed in"
    c.app.register_blueprint(bp)


def db():
    return core.db()


def render(template, **ctx):
    return render_template(template, noindex=True, **ctx)


def now_str(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def norm_email(raw):
    return (raw or "").strip().lower()[:200]


def safe_next(url):
    """Only allow redirects to paths on this site (no open redirect)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


def valid_password(pw, email=""):
    if len(pw) < 8:
        return "Use at least 8 characters for your password."
    if len(pw) > 128:
        return "That password is too long (128 characters max)."
    if pw.lower() in COMMON_PASSWORDS or pw.isdigit() and len(set(pw)) < 4:
        return "That password is too easy to guess. Try something longer or less common."
    if email and pw.lower() == email.lower():
        return "Your password can’t be the same as your email."
    return None


# --------------------------------------------------------------------------
# Session / current customer
# --------------------------------------------------------------------------
def current_customer():
    """The logged-in customer row (or None). Cached per request."""
    if "customer" not in g:
        cust = None
        cid = session.get("customer_id")
        if cid:
            row = db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
            if row and row["pw_version"] == session.get("pwv"):
                cust = row
            else:                                   # deleted, or password changed elsewhere -> signed out
                _drop_login_state()
        g.customer = cust
    return g.customer


def _drop_login_state():
    session.pop("customer_id", None)
    session.pop("pwv", None)
    session.permanent = False
    if session.get("cart_src") == "account":
        for k in ("cart_email", "cart_token", "cart_src"):
            session.pop(k, None)


def login_customer(row, remember=False):
    session["customer_id"] = row["id"]
    session["pwv"] = row["pw_version"]
    session.permanent = bool(remember)
    db().execute("UPDATE customers SET last_login_at=? WHERE id=?", (now_str(), row["id"]))
    db().commit()
    g.pop("customer", None)
    core._carts.on_login(row)


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not current_customer():
            flash("Please log in to see your account.", "error")
            return redirect(url_for("acct.login", next=request.full_path.rstrip("?")))
        return view(*a, **kw)
    return wrapper


# --------------------------------------------------------------------------
# Tokens (email verification + password reset). Only a hash is stored.
# --------------------------------------------------------------------------
def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(customer_id, purpose, hours):
    raw = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("DELETE FROM auth_tokens WHERE expires_at < ? OR (customer_id=? AND purpose=?)",
                 (now_str(), customer_id, purpose))          # tidy up + invalidate older links
    conn.execute("INSERT INTO auth_tokens (customer_id, purpose, token_hash, expires_at) VALUES (?,?,?,?)",
                 (customer_id, purpose, _hash(raw), now_str(datetime.now(timezone.utc) + timedelta(hours=hours))))
    conn.commit()
    return raw


def peek_token(raw, purpose):
    row = db().execute("SELECT * FROM auth_tokens WHERE token_hash=? AND purpose=? AND used_at IS NULL "
                       "AND expires_at > ?", (_hash(raw or ""), purpose, now_str())).fetchone()
    return row["customer_id"] if row else None


def consume_token(raw, purpose):
    """Single use. Returns the customer id, or None if invalid/expired/already used."""
    cid = peek_token(raw, purpose)
    if cid is None:
        return None
    cur = db().execute("UPDATE auth_tokens SET used_at=? WHERE token_hash=? AND purpose=? AND used_at IS NULL",
                       (now_str(), _hash(raw), purpose))
    db().commit()
    return cid if cur.rowcount == 1 else None


# --------------------------------------------------------------------------
# Emails
# --------------------------------------------------------------------------
def _shop():
    return core.load_json("site.json")["shop_name"]


def send_verification(cust):
    raw = issue_token(cust["id"], "verify", 72)
    link = core.abs_url(f"/account/verify/{raw}")
    core.send_email(cust["email"], f"Confirm your email — {_shop()}",
                    f"Hi {cust['name'] or 'there'},\n\nPlease confirm your email address to finish setting up your "
                    f"{_shop()} account and see your past orders:\n\n{link}\n\n"
                    f"This link works for 3 days. If you didn’t create an account, you can ignore this email.\n\n— {_shop()}",
                    reply_to=core.owner_email())


def send_reset(cust):
    raw = issue_token(cust["id"], "reset", 1)
    link = core.abs_url(f"/account/reset/{raw}")
    core.send_email(cust["email"], f"Reset your password — {_shop()}",
                    f"Hi {cust['name'] or 'there'},\n\nSomeone (hopefully you) asked to reset the password for your "
                    f"{_shop()} account. Choose a new one here:\n\n{link}\n\n"
                    f"This link works for 1 hour and can be used once. If you didn’t ask for this, ignore this email — "
                    f"your password has not changed.\n\n— {_shop()}", reply_to=core.owner_email())


def send_password_changed(cust):
    core.send_email(cust["email"], f"Your {_shop()} password was changed",
                    f"Hi {cust['name'] or 'there'},\n\nThe password for your {_shop()} account was just changed. "
                    f"If this was you, nothing more to do. If not, reset it right away at "
                    f"{core.abs_url('/account/forgot')} and contact us.\n\n— {_shop()}", reply_to=core.owner_email())


# --------------------------------------------------------------------------
# Data helpers (also used by checkout and the admin)
# --------------------------------------------------------------------------
def checkout_prefill(cust):
    return {"name": cust["name"] or "", "email": cust["email"], "phone": cust["phone"] or "",
            "address": cust["address"] or "", "city": cust["city"] or "", "state": cust["state"] or "",
            "pincode": cust["pincode"] or "", "save_address": "1"}


def save_address(customer_id, form):
    conn = db()
    conn.execute("UPDATE customers SET address=?, city=?, state=?, pincode=?, "
                 "phone=COALESCE(NULLIF(phone,''), ?), name=COALESCE(NULLIF(name,''), ?) WHERE id=?",
                 (form["address"], form["city"], form["state"], form["pincode"], form["phone"], form["name"],
                  customer_id))
    conn.commit()


def _order_predicate(cust):
    """Orders a customer may see: ones placed while logged in, plus guest orders on their *verified* email."""
    if cust["email_verified"]:
        return "(customer_id=? OR (customer_id IS NULL AND lower(email)=?))", (cust["id"], cust["email"])
    return "customer_id=?", (cust["id"],)


def customer_orders(cust, limit=100):
    pred, args = _order_predicate(cust)
    rows = db().execute(f"SELECT * FROM orders WHERE {pred} AND status!='pending_payment' ORDER BY id DESC LIMIT ?",
                        args + (limit,)).fetchall()
    return [dict(r, number=1000 + r["id"]) for r in rows]


def delete_customer(conn, customer_id):
    """Remove the account and its personal data; past orders are kept (for accounting) but unlinked."""
    row = conn.execute("SELECT email FROM customers WHERE id=?", (customer_id,)).fetchone()
    conn.execute("UPDATE orders SET customer_id=NULL WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM carts WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM auth_tokens WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    conn.commit()
    return row["email"] if row else None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_customer():
        return redirect(url_for("acct.home"))
    nxt = safe_next(request.values.get("next"))
    if request.method == "POST":
        email, pw = norm_email(request.form.get("email")), request.form.get("password", "")
        ip = request.remote_addr or "?"
        if not core.throttle(f"login-ip:{ip}", 20, 900) or not core.throttle(f"login-em:{email}", 8, 900):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return render("account_login.html", email=email, next=nxt, page_title="Log in"), 429
        row = db().execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
        ok = len(pw) <= 128 and check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw) and row
        if ok:
            login_customer(row, remember=request.form.get("remember") == "1")
            return redirect(nxt or url_for("acct.home"))
        flash("That email and password don’t match.", "error")
        return render("account_login.html", email=email, next=nxt, page_title="Log in"), 401
    return render("account_login.html", email=request.args.get("email", ""), next=nxt, page_title="Log in")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_customer():
        return redirect(url_for("acct.home"))
    nxt = safe_next(request.values.get("next"))
    form = {"reminders": "1", "email": request.args.get("email", "")}
    if request.method == "POST":
        f = request.form
        form = {"name": f.get("name", "").strip()[:100], "email": norm_email(f.get("email")),
                "phone": f.get("phone", "").strip()[:20], "reminders": "1" if f.get("reminders") == "1" else "0"}
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        errors = []
        if len(form["name"]) < 2:
            errors.append("Please enter your name.")
        if not core.EMAIL_RE.match(form["email"]):
            errors.append("Please enter a valid email address.")
        if form["phone"] and not core.PHONE_RE.match(form["phone"]):
            errors.append("Enter a 10-digit Indian mobile number, or leave the phone blank.")
        bad = valid_password(pw, form["email"])
        if bad:
            errors.append(bad)
        elif pw != pw2:
            errors.append("The two passwords don’t match.")
        if not core.throttle(f"register-ip:{request.remote_addr}", 10, 3600):
            errors.append("Too many sign-ups from this connection. Please try again later.")
        if not errors and db().execute("SELECT 1 FROM customers WHERE email=?", (form["email"],)).fetchone():
            errors.append("An account with this email already exists. Please log in, or reset your password.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render("account_register.html", form=form, next=nxt, page_title="Create account"), 400
        conn = db()
        cur = conn.execute("INSERT INTO customers (email, name, phone, password_hash, cart_reminders) VALUES (?,?,?,?,?)",
                           (form["email"], form["name"], form["phone"], generate_password_hash(pw),
                            1 if form["reminders"] == "1" else 0))
        conn.commit()
        row = conn.execute("SELECT * FROM customers WHERE id=?", (cur.lastrowid,)).fetchone()
        login_customer(row)
        send_verification(row)
        flash(f"Welcome, {row['name'].split()[0]}! We’ve emailed a link to {row['email']} — confirm it to see past orders.")
        return redirect(nxt or url_for("acct.home"))
    return render("account_register.html", form=form, next=nxt, page_title="Create account")


@bp.post("/logout")
def logout():
    _drop_login_state()
    g.pop("customer", None)
    flash("You’re logged out.")
    return redirect(url_for("index"))


@bp.route("", strict_slashes=False)
@login_required
def home():
    cust = current_customer()
    return render("account_home.html", orders=customer_orders(cust, 20), page_title="My account")


@bp.route("/orders/<int:number>")
@login_required
def order(number):
    cust = current_customer()
    row = db().execute("SELECT * FROM orders WHERE id=?", (number - 1000,)).fetchone() if number > 1000 else None
    pred, args = _order_predicate(cust)
    owned = row and db().execute(f"SELECT 1 FROM orders WHERE id=? AND {pred} AND status!='pending_payment'",
                                 (row["id"],) + args).fetchone()
    if not owned:
        abort(404)
    return render("account_order.html", order=row, items=json.loads(row["items_json"]),
                  steps=["new", "confirmed", "shipped", "delivered"], page_title=f"Order #{number}")


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    cust = current_customer()
    if request.method == "POST":
        f, action, conn = request.form, request.form.get("action"), db()
        problem = None
        if action == "details":
            name, phone = f.get("name", "").strip()[:100], f.get("phone", "").strip()[:20]
            if len(name) < 2:
                problem = "Please enter your name."
            elif phone and not core.PHONE_RE.match(phone):
                problem = "Enter a 10-digit Indian mobile number, or leave it blank."
            else:
                conn.execute("UPDATE customers SET name=?, phone=? WHERE id=?", (name, phone, cust["id"]))
                flash("Details saved.")
        elif action == "address":
            a = {k: f.get(k, "").strip()[:200] for k in ("address", "city", "state", "pincode")}
            if not any(a.values()):
                conn.execute("UPDATE customers SET address='', city='', state='', pincode='' WHERE id=?", (cust["id"],))
                flash("Saved address removed.")
            elif len(a["address"]) < 6 or len(a["city"]) < 2 or len(a["state"]) < 2 or not re.fullmatch(r"\d{6}", a["pincode"]):
                problem = "Please fill in the full address, with a 6-digit PIN code."
            else:
                conn.execute("UPDATE customers SET address=?, city=?, state=?, pincode=? WHERE id=?",
                             (a["address"], a["city"], a["state"], a["pincode"], cust["id"]))
                flash("Address saved — we’ll fill it in at checkout.")
        elif action == "reminders":
            core._carts.set_reminders(cust, f.get("cart_reminders") == "1")
            flash("Email preference saved.")
        elif action == "password":
            cur_pw, new_pw, new2 = f.get("current_password", ""), f.get("new_password", ""), f.get("new_password2", "")
            if not core.throttle(f"pwchange:{cust['id']}", 8, 900):
                problem = "Too many attempts. Please wait a few minutes."
            elif len(cur_pw) > 128 or not check_password_hash(cust["password_hash"], cur_pw):
                problem = "Your current password isn’t right."
            elif valid_password(new_pw, cust["email"]):
                problem = valid_password(new_pw, cust["email"])
            elif new_pw != new2:
                problem = "The two new passwords don’t match."
            else:
                conn.execute("UPDATE customers SET password_hash=?, pw_version=pw_version+1 WHERE id=?",
                             (generate_password_hash(new_pw), cust["id"]))
                conn.commit()
                fresh = conn.execute("SELECT * FROM customers WHERE id=?", (cust["id"],)).fetchone()
                session["pwv"] = fresh["pw_version"]            # keep this device signed in; others are signed out
                send_password_changed(fresh)
                flash("Password changed. Other devices have been signed out.")
        if problem:
            flash(problem, "error")
            g.pop("customer", None)
            return render("account_profile.html", page_title="Profile"), 400
        conn.commit()
        g.pop("customer", None)
        return redirect(url_for("acct.profile"))
    return render("account_profile.html", page_title="Profile")


@bp.post("/delete")
@login_required
def delete():
    cust = current_customer()
    pw = request.form.get("password", "")
    if not core.throttle(f"pwchange:{cust['id']}", 8, 900) or len(pw) > 128 or not check_password_hash(cust["password_hash"], pw):
        flash("Password not right — your account was not deleted.", "error")
        return redirect(url_for("acct.profile"))
    delete_customer(db(), cust["id"])
    _drop_login_state()
    g.pop("customer", None)
    flash("Your account and saved details were deleted. Past orders are kept only for our accounting records.")
    return redirect(url_for("index"))


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    sent = False
    if request.method == "POST":
        email = norm_email(request.form.get("email"))
        ip_ok = core.throttle(f"forgot-ip:{request.remote_addr}", 6, 3600)
        em_ok = core.throttle(f"forgot-em:{email}", 3, 3600)
        if ip_ok and em_ok and core.EMAIL_RE.match(email):
            row = db().execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
            if row:
                send_reset(row)
        sent = True                                    # same answer whether or not the account exists
        flash("If an account exists for that email, a reset link is on its way. Check your inbox (and spam).")
    return render("account_forgot.html", sent=sent, page_title="Forgot password")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    cid = peek_token(token, "reset")
    if cid is None:
        return render("message.html", title="This link has expired",
                      text="Password reset links work once, for one hour. You can ask for a new one.",
                      link=url_for("acct.forgot"), link_label="Send a new link", page_title="Link expired"), 410
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        row = db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        problem = valid_password(pw, row["email"]) or (None if pw == pw2 else "The two passwords don’t match.")
        if problem:
            flash(problem, "error")
            return render("account_reset.html", token=token, page_title="Choose a new password"), 400
        if consume_token(token, "reset") is None:
            abort(410)
        db().execute("UPDATE customers SET password_hash=?, pw_version=pw_version+1, email_verified=1 WHERE id=?",
                     (generate_password_hash(pw), cid))
        db().commit()
        send_password_changed(row)
        flash("Password updated. Please log in with your new password.")
        return redirect(url_for("acct.login"))
    return render("account_reset.html", token=token, page_title="Choose a new password")


@bp.route("/verify/<token>")
def verify(token):
    cid = consume_token(token, "verify")
    if cid is None:
        return render("message.html", title="That link didn’t work",
                      text="It may have expired or been used already. Log in and request a new confirmation email from your account page.",
                      link=url_for("acct.login"), link_label="Log in", page_title="Confirm email"), 410
    db().execute("UPDATE customers SET email_verified=1 WHERE id=?", (cid,))
    db().commit()
    g.pop("customer", None)
    return render("message.html", title="Email confirmed ✓",
                  text="Thanks! Your past orders placed with this email will now show in your account.",
                  link=url_for("acct.home"), link_label="Go to my account", page_title="Email confirmed")


@bp.post("/resend-verification")
@login_required
def resend_verification():
    cust = current_customer()
    if cust["email_verified"]:
        flash("Your email is already confirmed.")
    elif not core.throttle(f"resend:{cust['id']}", 3, 3600):
        flash("Please wait a bit before asking for another email.", "error")
    else:
        send_verification(cust)
        flash(f"Confirmation email sent to {cust['email']}.")
    return redirect(url_for("acct.home"))
