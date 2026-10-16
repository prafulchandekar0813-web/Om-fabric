"""Abandoned-cart reminders (Flask blueprint + job).

Only carts whose owner has *consented* are tracked:
  * logged-in customers who keep "cart reminders" on in their profile, and
  * guests who tick "Email me a reminder…" at checkout.
Every email has a one-click unsubscribe link (and List-Unsubscribe headers). Unsubscribing stops
reminder emails to that address; order receipts and password emails are unaffected.
"""
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from itsdangerous import BadSignature, URLSafeSerializer

try:
    from zoneinfo import ZoneInfo
except ImportError:                                  # pragma: no cover
    ZoneInfo = None

core = None
bp = Blueprint("carts", __name__)
DEFAULTS = {"enabled": True, "first_after_hours": 2, "send_second": True,
            "second_after_hours": 24, "discount_code": ""}
STALE_DAYS = 7                  # carts untouched this long are retired, never emailed
MAX_FAILS = 3                   # give up on an address after this many failed sends
MIN_GAP_HOURS = 72              # never email the same address more than once in this window


def init(c):
    global core
    core = c
    c.app.register_blueprint(bp)
    if os.environ.get("ENABLE_SCHEDULER") == "1":
        threading.Thread(target=_scheduler_loop, daemon=True, name="cart-reminders").start()


def _scheduler_loop():
    while True:
        time.sleep(600)
        try:
            with core.app.app_context():
                run_job()
        except Exception:
            core.log.exception("Abandoned-cart job crashed")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def now_str(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def config():
    d = dict(DEFAULTS)
    d.update(core.load_json("site.json").get("abandoned_cart", {}))
    return d


def suppressed(email):
    return bool(core.db().execute("SELECT 1 FROM email_suppressions WHERE email=?", (email.lower(),)).fetchone())


def store_tz():
    name = os.environ.get("STORE_TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


def in_quiet_hours(now):
    h = now.astimezone(store_tz()).hour
    return h >= 22 or h < 8


# --------------------------------------------------------------------------
# Tracking the visitor's cart (only after consent)
# --------------------------------------------------------------------------
def track_cart():
    """Mirror the session cart into the carts table, if we're allowed to email this visitor."""
    email = session.get("cart_email")
    if not email or suppressed(email):
        return
    conn = core.db()
    cart = session.get("cart") or {}
    token = session.get("cart_token")
    row = conn.execute("SELECT * FROM carts WHERE token=?", (token,)).fetchone() if token else None
    now = now_str()
    if not cart:
        if row and row["status"] == "active":
            conn.execute("UPDATE carts SET status='emptied', updated_at=? WHERE id=?", (now, row["id"]))
            conn.commit()
        return
    variants = core.catalog()["variants"]
    items = {vid: q for vid, q in cart.items() if vid in variants}
    subtotal = sum(variants[v][1]["price"] * q for v, q in items.items())
    cust = core._accounts.current_customer()
    cid = cust["id"] if cust else None
    if row is None:
        token = secrets.token_urlsafe(16)
        session["cart_token"] = token
        conn.execute("INSERT INTO carts (token, restore_token, email, customer_id, items_json, subtotal, status, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,'active',?,?)",
                     (token, secrets.token_urlsafe(24), email.lower(), cid, json.dumps(items), subtotal, now, now))
    elif row["status"] in ("unsubscribed", "recovered"):
        return                                           # respect opt-outs; recovered carts are finished
    else:
        fresh_start = row["status"] in ("emptied", "expired")
        conn.execute("UPDATE carts SET email=?, customer_id=?, items_json=?, subtotal=?, updated_at=?, status='active'"
                     + (", reminders_sent=0, last_reminder_at=NULL, fail_count=0, reminder_code=NULL" if fresh_start else "")
                     + " WHERE id=?", (email.lower(), cid, json.dumps(items), subtotal, now, row["id"]))
    conn.commit()


def capture_guest(email):
    """A guest ticked 'email me a reminder'. Returns True if we're now tracking their cart."""
    email = (email or "").strip().lower()[:200]
    if not core.EMAIL_RE.match(email) or not session.get("cart"):
        return False
    conn = core.db()
    conn.execute("DELETE FROM email_suppressions WHERE email=?", (email,))      # explicit, fresh consent
    session["cart_email"], session["cart_src"] = email, "guest"
    token = session.get("cart_token")
    if token:
        conn.execute("UPDATE carts SET status='active' WHERE token=? AND status='unsubscribed'", (token,))
    conn.commit()
    track_cart()
    return True


def withdraw_guest():
    token = session.get("cart_token")
    if session.get("cart_src") == "guest":
        if token:
            core.db().execute("UPDATE carts SET status='unsubscribed' WHERE token=? AND status='active'", (token,))
            core.db().commit()
        for k in ("cart_email", "cart_src"):
            session.pop(k, None)


def load_items(row):
    """Put the (still available) items of a saved cart into this visitor's bag. Returns how many lines."""
    variants = core.catalog()["variants"]
    cart = dict(session.get("cart", {}))
    added = 0
    for vid, qty in json.loads(row["items_json"]).items():
        found = variants.get(vid)
        if not found or not core.is_available(found[1]):
            continue
        left = core.stock_of(found[1])
        qty = min(int(qty), core.MAX_QTY, left if left is not None else core.MAX_QTY)
        if qty > 0:
            cart[vid] = max(cart.get(vid, 0), qty)
            added += 1
    session["cart"] = cart
    return added


def on_login(cust):
    """Account preference decides whether this session's cart is tracked; bring back an open cart."""
    stale = session.get("cart_token")
    if stale:                       # a leftover guest bag (other address / opted out) must not block the account's bag
        prev = core.db().execute("SELECT email, status FROM carts WHERE token=?", (stale,)).fetchone()
        if not prev or prev["email"] != cust["email"] or prev["status"] in ("unsubscribed", "recovered"):
            session.pop("cart_token", None)
    if cust["cart_reminders"] and not suppressed(cust["email"]):
        session["cart_email"], session["cart_src"] = cust["email"], "account"
        if not session.get("cart"):
            row = core.db().execute("SELECT * FROM carts WHERE email=? AND status='active' "
                                    "ORDER BY updated_at DESC LIMIT 1", (cust["email"],)).fetchone()
            if row and load_items(row):
                session["cart_token"] = row["token"]
        track_cart()
    else:
        for k in ("cart_email", "cart_src", "cart_token"):
            session.pop(k, None)


def set_reminders(cust, on):
    conn = core.db()
    conn.execute("UPDATE customers SET cart_reminders=? WHERE id=?", (1 if on else 0, cust["id"]))
    if on:
        conn.execute("DELETE FROM email_suppressions WHERE email=?", (cust["email"],))
        conn.execute("UPDATE carts SET status='active' WHERE token=? AND status='unsubscribed'",
                     (session.get("cart_token") or "-",))              # the bag they're holding right now
        session["cart_email"], session["cart_src"] = cust["email"], "account"
        conn.commit()
        track_cart()
    else:
        conn.execute("UPDATE carts SET status='unsubscribed' WHERE email=? AND status='active'", (cust["email"],))
        if session.get("cart_src") == "account":
            session.pop("cart_email", None)
            session.pop("cart_src", None)
        conn.commit()


def mark_recovered(order_id, email):
    """An order was placed: the visitor's tracked carts are no longer 'abandoned'."""
    core.db().execute("UPDATE carts SET status='recovered', recovered_order_id=? "
                      "WHERE status='active' AND (token=? OR email=?)",
                      (order_id, session.get("cart_token") or "-", (email or "").lower()))
    core.db().commit()
    if session.get("cart_src") == "guest":               # a guest's consent was for finishing *this* order
        session.pop("cart_email", None)
        session.pop("cart_src", None)


def do_unsubscribe(email):
    email = email.lower()
    conn = core.db()
    conn.execute("INSERT OR IGNORE INTO email_suppressions (email, reason) VALUES (?, 'unsubscribe link')", (email,))
    conn.execute("UPDATE carts SET status='unsubscribed' WHERE email=? AND status='active'", (email,))
    conn.execute("UPDATE customers SET cart_reminders=0 WHERE email=?", (email,))
    conn.commit()


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------
def _serializer():
    return URLSafeSerializer(core.app.config["SECRET_KEY"], salt="cart-unsubscribe")


def unsubscribe_link(base, email):
    return f"{base}/unsubscribe/{_serializer().dumps(email.lower())}"


@bp.post("/cart/remind")
def remind():
    if core._accounts.current_customer():
        return jsonify(ok=True, managed="account")
    if not core.throttle(f"remind:{request.remote_addr}", 30, 3600):
        return jsonify(ok=False), 429
    if request.form.get("consent") == "1":
        return jsonify(ok=capture_guest(request.form.get("email", "")))
    withdraw_guest()
    return jsonify(ok=True)


@bp.route("/cart/restore/<token>")
def restore(token):
    conn = core.db()
    row = conn.execute("SELECT * FROM carts WHERE restore_token=?", (token,)).fetchone()
    if not row or row["status"] == "recovered":
        flash("That bag link has expired — or the order was already placed.", "error")
        return redirect(url_for("cart"))
    added = load_items(row)
    conn.execute("UPDATE carts SET clicked_at=? WHERE id=?", (now_str(), row["id"]))
    conn.commit()
    if not added:
        flash("Sorry — the items from that bag are no longer available.", "error")
        return redirect(url_for("collection", handle="all"))
    cust = core._accounts.current_customer()
    if row["status"] in ("active", "emptied", "expired") and (not cust or cust["email"] == row["email"]):
        session["cart_email"], session["cart_token"] = row["email"], row["token"]
        session.setdefault("cart_src", "account" if cust else "guest")
    if row["reminder_code"]:
        subtotal = sum(l["line_price"] for l in core.cart_lines())
        d, _err = core.check_discount(row["reminder_code"], subtotal, row["email"])
        if d:
            session["discount"] = row["reminder_code"]
    flash("Welcome back — your bag is ready.")
    return redirect(url_for("cart"))


def _email_from_token(token):
    try:
        return _serializer().loads(token)
    except BadSignature:
        abort(404)


@bp.get("/unsubscribe/<token>")
def unsubscribe_page(token):
    email = _email_from_token(token)
    return render_template("unsubscribe.html", token=token, email=email, done=False, noindex=True,
                           page_title="Unsubscribe")


@bp.post("/unsubscribe/<token>")
def unsubscribe_post(token):
    email = _email_from_token(token)                    # token-authenticated, so exempt from CSRF
    do_unsubscribe(email)
    return render_template("unsubscribe.html", token=token, email=email, done=True, noindex=True,
                           page_title="Unsubscribed")


@bp.route("/tasks/abandoned-carts", methods=["GET", "POST"])
def task():
    """For cron services: call with header X-Cron-Key (or ?key=) equal to CRON_SECRET."""
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        abort(404)
    if not core.throttle(f"cron-fail:{request.remote_addr}", 30, 3600):
        abort(429)
    key = request.headers.get("X-Cron-Key") or request.values.get("key", "")
    if not hmac.compare_digest(key.encode(), secret.encode()):
        abort(403)
    return jsonify(run_job())


# --------------------------------------------------------------------------
# The reminder job
# --------------------------------------------------------------------------
def _image_url(base, path):
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return base + (path if path.startswith("/") else "/static/" + path)


def build_lines(row, base):
    variants = core.catalog()["variants"]
    lines = []
    for vid, qty in json.loads(row["items_json"]).items():
        found = variants.get(vid)
        if not found or not core.is_available(found[1]):
            continue
        product, variant = found
        left = core.stock_of(variant)
        qty = min(int(qty), left if left is not None else int(qty))
        if qty <= 0:
            continue
        lines.append({"name": product["title"], "option": "" if variant["title"] == "Default Title" else variant["title"],
                      "qty": qty, "price": variant["price"], "total": variant["price"] * qty,
                      "image": _image_url(base, product.get("featured_image")), "url": base + product["url"]})
    return lines


def describe_discount(d):
    if d["type"] == "percent":
        text = f"{d['value']}% off"
    elif d["type"] == "flat":
        text = f"{core.money(d['value'])} off"
    else:
        text = "free shipping"
    if d.get("min_order"):
        text += f" orders over {core.money(d['min_order'])}"
    return text


def build_email(row, lines, number, code, base):
    site = core.load_json("site.json")
    shop = site["shop_name"]
    name = None
    if row["customer_id"]:
        c = core.db().execute("SELECT name FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        name = (c["name"] or "").split()[0] if c and c["name"] else None
    restore_url = f"{base}/cart/restore/{row['restore_token']}"
    unsub = unsubscribe_link(base, row["email"])
    subtotal = sum(l["total"] for l in lines)
    biz = site.get("business", {})
    footer = " · ".join(x for x in [biz.get("legal_name") or shop, biz.get("address")] if x)
    subject = (f"You left something in your bag at {shop}" if number == 1
               else f"Still thinking it over? Your {shop} bag is waiting")
    code_text = describe_discount(code) if code else None
    body = [f"Hi {name or 'there'},", "",
            f"You left {'this' if len(lines) == 1 else 'these'} in your bag at {shop}:", ""]
    for l in lines:
        body.append(f"  {l['qty']} x {l['name']}" + (f" ({l['option']})" if l["option"] else "") + f" — {core.money(l['total'])}")
    body += ["", f"Subtotal: {core.money(subtotal)}", "", f"Pick up where you left off: {restore_url}"]
    if code:
        body += ["", f"Use code {code['code']} at checkout for {code_text}. It will be added when you use the link above."]
    body += ["", "Items are only held while stock lasts.", "",
             f"You’re getting this because you asked for cart reminders. Unsubscribe: {unsub}", footer]
    html = core.app.jinja_env.get_template("email_cart.html").render(
        shop=shop, name=name, lines=lines, subtotal=subtotal, restore_url=restore_url, code=code, code_text=code_text,
        unsub_url=unsub, footer=footer, accent=site.get("background_color", "#f7f5f0"))
    return subject, "\n".join(body), html, unsub


def run_job(now=None):
    """Send due reminders. Safe to run often and from several processes (each cart is claimed atomically)."""
    cfg = config()
    now = now or datetime.now(timezone.utc)
    if not cfg["enabled"]:
        return {"skipped": "Cart reminders are switched off in Settings."}
    if not core.smtp_configured():
        return {"skipped": "Email isn’t set up yet (SMTP_HOST)."}
    base = core.site_url()
    if not base:
        return {"skipped": "SITE_URL isn’t set, so links in the emails would be broken."}
    if in_quiet_hours(now):
        return {"skipped": "Quiet hours (10pm–8am IST) — reminders go out later."}
    conn = core.db()
    res = {"sent": 0, "recovered": 0, "retired": 0, "failed": 0}
    max_n = 2 if cfg["send_second"] else 1
    for row in conn.execute("SELECT * FROM carts WHERE status='active' ORDER BY updated_at").fetchall():
        age_h = (now - parse_ts(row["updated_at"])).total_seconds() / 3600
        n = row["reminders_sent"]
        if age_h > 24 * STALE_DAYS:
            conn.execute("UPDATE carts SET status='expired' WHERE id=?", (row["id"],))
            res["retired"] += 1
            continue
        if n >= max_n or row["fail_count"] >= MAX_FAILS:
            continue
        if age_h < (cfg["first_after_hours"] if n == 0 else cfg["second_after_hours"]):
            continue
        if n and row["last_reminder_at"] and now - parse_ts(row["last_reminder_at"]) < timedelta(hours=12):
            continue
        bought = conn.execute("SELECT id FROM orders WHERE status!='cancelled' AND created_at >= ? AND "
                              "(lower(email)=? OR (customer_id IS NOT NULL AND customer_id=?))",
                              (row["updated_at"], row["email"].lower(), row["customer_id"] or -1)).fetchone()
        if bought:
            conn.execute("UPDATE carts SET status='recovered', recovered_order_id=? WHERE id=?", (bought["id"], row["id"]))
            res["recovered"] += 1
            continue
        if suppressed(row["email"]):
            conn.execute("UPDATE carts SET status='unsubscribed' WHERE id=?", (row["id"],))
            continue
        recent = conn.execute("SELECT 1 FROM carts WHERE email=? AND last_reminder_at > ? AND id!=?",
                              (row["email"], now_str(now - timedelta(hours=MIN_GAP_HOURS)), row["id"])).fetchone()
        if recent:
            continue
        lines = build_lines(row, base)
        if not lines:
            conn.execute("UPDATE carts SET status='expired' WHERE id=?", (row["id"],))
            res["retired"] += 1
            continue
        code = None
        if n == 1 and cfg["discount_code"]:
            code, _err = core.check_discount(cfg["discount_code"], sum(l["total"] for l in lines), row["email"])
        claim = conn.execute("UPDATE carts SET reminders_sent=reminders_sent+1, last_reminder_at=?, reminder_code=? "
                             "WHERE id=? AND reminders_sent=? AND status='active'",
                             (now_str(now), code["code"] if code else None, row["id"], n))
        conn.commit()
        if claim.rowcount != 1:                         # another worker got there first
            continue
        subject, text, html, unsub = build_email(row, lines, n + 1, code, base)
        ok = core.send_email_now(row["email"], subject, text, reply_to=core.owner_email(), html=html,
                                 headers={"List-Unsubscribe": f"<{unsub}>",
                                          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        if ok:
            res["sent"] += 1
        else:                                           # put it back so a later run can retry (max 3 tries)
            conn.execute("UPDATE carts SET reminders_sent=?, last_reminder_at=?, fail_count=fail_count+1 WHERE id=?",
                         (n, row["last_reminder_at"], row["id"]))
            res["failed"] += 1
        conn.commit()
    conn.commit()
    return res


def stats():
    conn = core.db()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]          # noqa: E731
    recovered = one("SELECT COUNT(*) FROM carts WHERE status='recovered' AND reminders_sent>0")
    return {"active": one("SELECT COUNT(*) FROM carts WHERE status='active'"),
            "emails_sent": one("SELECT COALESCE(SUM(reminders_sent),0) FROM carts"),
            "reminded": one("SELECT COUNT(*) FROM carts WHERE reminders_sent>0"),
            "recovered": recovered,
            "revenue": one("SELECT COALESCE(SUM(o.total),0) FROM carts c JOIN orders o ON o.id=c.recovered_order_id "
                           "WHERE c.status='recovered' AND c.reminders_sent>0 AND o.status!='cancelled'"),
            "suppressed": one("SELECT COUNT(*) FROM email_suppressions")}
