"""Thread & Co. — standalone storefront (Flask).

Replaces the Shopify theme's backend: catalogue, cart, checkout (COD),
contact form and a small password-protected admin view.

Content lives in data/*.json. Orders and messages are stored in SQLite.
"""
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "store.db"))
PER_PAGE = 12
MAX_QTY = 20
ORDER_STATUSES = ["new", "confirmed", "shipped", "delivered", "cancelled"]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)
log = logging.getLogger("threadandco")


# --------------------------------------------------------------------------
# Content loading (re-read automatically when a JSON file changes)
# --------------------------------------------------------------------------
_cache = {}


def load_json(name):
    path = DATA_DIR / name
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if not hit or hit[0] != mtime:
        with open(path, encoding="utf-8") as f:
            _cache[name] = (mtime, json.load(f))
    return _cache[name][1]


def catalog():
    """Products with the derived fields the templates use."""
    hit = _cache.get("_catalog")
    mtime = (DATA_DIR / "products.json").stat().st_mtime
    if hit and hit[0] == mtime:
        return hit[1]
    raw = load_json("products.json")
    by_handle, variants = {}, {}
    for p in raw["products"]:
        vs = p["variants"]
        p["price"] = min(v["price"] for v in vs)
        p["compare_at_max"] = max((v.get("compare_at_price") or 0) for v in vs)
        p["available"] = any(v.get("available", True) for v in vs)
        p["has_only_default_variant"] = len(vs) == 1 and vs[0]["title"] == "Default Title"
        p["featured_image"] = p["images"][0] if p.get("images") else None
        p["url"] = f"/products/{p['handle']}"
        by_handle[p["handle"]] = p
        for v in vs:
            variants[v["id"]] = (p, v)
    cat = {"collections": {c["handle"]: c for c in raw["collections"]},
           "products": raw["products"], "by_handle": by_handle, "variants": variants}
    _cache["_catalog"] = (mtime, cat)
    return cat


def collection_products(handle):
    cat = catalog()
    if handle == "all":
        return cat["products"]
    return [p for p in cat["products"] if handle in p.get("collections", [])]


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
def money(n):
    """₹ with Indian digit grouping, e.g. 123456 -> ₹1,23,456"""
    n = int(n)
    s = str(abs(n))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + "₹" + s


def img_url(path):
    if not path:
        return ""
    if path.startswith(("http://", "https://", "/")):
        return path
    return url_for("static", filename=path)


app.jinja_env.filters["money"] = money
app.jinja_env.filters["img_url"] = img_url
app.jinja_env.filters["nl2br"] = lambda s: re.sub(r"\n", "<br>", s)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


# --------------------------------------------------------------------------
# Cart (stored in the signed session cookie: {variant_id: quantity})
# --------------------------------------------------------------------------
def cart_lines():
    cat = catalog()
    lines = []
    for vid, qty in session.get("cart", {}).items():
        found = cat["variants"].get(vid)
        if not found:
            continue
        product, variant = found
        lines.append({"product": product, "variant": variant, "quantity": qty,
                      "line_price": variant["price"] * qty})
    return lines


def cart_totals(lines):
    site = load_json("site.json")["shipping"]
    subtotal = sum(l["line_price"] for l in lines)
    shipping = 0 if (not lines or subtotal >= site["free_over"]) else site["flat_rate"]
    return {"subtotal": subtotal, "shipping": shipping, "total": subtotal + shipping,
            "count": sum(l["quantity"] for l in lines),
            "free_over": site["free_over"]}


@app.context_processor
def inject_globals():
    site = load_json("site.json")
    count = sum(session.get("cart", {}).values())
    return {"site": site, "cart_count": count, "csrf_token": csrf_token,
            "now_year": datetime.now().year}


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Please go back, refresh and try again.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# --------------------------------------------------------------------------
# Database (orders + contact messages)
# --------------------------------------------------------------------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'new',
            name TEXT, email TEXT, phone TEXT,
            address TEXT, city TEXT, state TEXT, pincode TEXT, notes TEXT,
            payment_method TEXT,
            items_json TEXT, subtotal INTEGER, shipping INTEGER, total INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            name TEXT, email TEXT, topic TEXT, body TEXT
        );
    """)
    conn.commit()
    conn.close()


init_db()


def notify(subject, body):
    """Email the shop owner if SMTP_* env vars are set; otherwise just log."""
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("NOTIFY_EMAIL") or load_json("site.json")["contact_email"]
    if not host:
        log.info("(no SMTP configured) %s", subject)
        return
    try:
        msg = EmailMessage()
        msg["Subject"], msg["To"] = subject, to
        msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or to
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=10) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    except Exception:  # never let an email problem break an order
        log.exception("Could not send notification email")


# --------------------------------------------------------------------------
# Storefront routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    site = load_json("site.json")
    feat = site["featured"]
    col = catalog()["collections"].get(feat["collection"])
    products = collection_products(feat["collection"])[: feat.get("limit", 4)] if col else []
    return render_template("index.html", hero=site["hero"], featured=feat,
                           featured_collection=col, products=products,
                           page_title="Home")


@app.route("/collections/<handle>")
def collection(handle):
    col = catalog()["collections"].get(handle)
    if not col:
        abort(404)
    items = collection_products(handle)
    pages = max(1, -(-len(items) // PER_PAGE))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    items = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    return render_template("collection.html", collection=col, products=items,
                           page=page, pages=pages, page_title=col["title"])


@app.route("/products/<handle>")
def product(handle):
    p = catalog()["by_handle"].get(handle)
    if not p:
        abort(404)
    first = next((v for v in p["variants"] if v.get("available", True)), p["variants"][0])
    return render_template("product.html", product=p, selected=first, page_title=p["title"])


@app.route("/pages/<handle>")
def page(handle):
    pg = load_json("pages.json").get(handle)
    if not pg:
        abort(404)
    return render_template("page.html", page=pg, page_title=pg["title"])


@app.route("/cart")
def cart():
    lines = cart_lines()
    return render_template("cart.html", lines=lines, totals=cart_totals(lines), page_title="Your bag")


@app.post("/cart/add")
def cart_add():
    vid = request.form.get("id", "")
    found = catalog()["variants"].get(vid)
    if not found:
        abort(404)
    _, variant = found
    if not variant.get("available", True):
        flash("Sorry, that option is sold out.", "error")
        return redirect(request.referrer or url_for("cart"))
    qty = max(1, min(request.form.get("quantity", 1, type=int) or 1, MAX_QTY))
    cart_data = dict(session.get("cart", {}))
    cart_data[vid] = min(cart_data.get(vid, 0) + qty, MAX_QTY)
    session["cart"] = cart_data
    return redirect(url_for("cart"))


@app.post("/cart/update")
def cart_update():
    cart_data = {}
    for vid in session.get("cart", {}):
        raw = request.form.get(f"qty_{vid}")
        qty = int(raw) if raw and raw.isdigit() else session["cart"][vid]
        if qty > 0:
            cart_data[vid] = min(qty, MAX_QTY)
    session["cart"] = cart_data
    if "checkout" in request.form and cart_data:
        return redirect(url_for("checkout"))
    return redirect(url_for("cart"))


PHONE_RE = re.compile(r"^(\+91[\s-]?)?[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    lines = cart_lines()
    if not lines:
        return redirect(url_for("cart"))
    totals = cart_totals(lines)
    methods = load_json("site.json")["payment_methods"]
    form, errors = {}, {}

    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ["name", "email", "phone", "address", "city", "state", "pincode", "notes", "payment"]}
        if request.form.get("website"):          # honeypot: bots fill this in
            return redirect(url_for("index"))
        if len(form["name"]) < 2: errors["name"] = "Please enter your name."
        if not EMAIL_RE.match(form["email"]): errors["email"] = "Please enter a valid email."
        if not PHONE_RE.match(form["phone"]): errors["phone"] = "Enter a 10-digit Indian mobile number."
        if len(form["address"]) < 6: errors["address"] = "Please enter your full address."
        if len(form["city"]) < 2: errors["city"] = "Please enter your city."
        if len(form["state"]) < 2: errors["state"] = "Please enter your state."
        if not re.fullmatch(r"\d{6}", form["pincode"]): errors["pincode"] = "Enter a 6-digit PIN code."
        method = next((m for m in methods if m["id"] == form["payment"]), None)
        if not method: errors["payment"] = "Please choose a payment method."
        for l in lines:
            if not l["variant"].get("available", True):
                errors["cart"] = f"{l['product']['title']} ({l['variant']['title']}) just sold out. Please update your bag."

        if not errors:
            items = [{"product": l["product"]["title"], "variant": l["variant"]["title"],
                      "variant_id": l["variant"]["id"], "quantity": l["quantity"],
                      "unit_price": l["variant"]["price"]} for l in lines]
            cur = db().execute(
                """INSERT INTO orders (name,email,phone,address,city,state,pincode,notes,
                   payment_method,items_json,subtotal,shipping,total)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (form["name"], form["email"], form["phone"], form["address"], form["city"],
                 form["state"], form["pincode"], form["notes"], method["label"],
                 json.dumps(items), totals["subtotal"], totals["shipping"], totals["total"]))
            db().commit()
            order_id = cur.lastrowid
            summary = "\n".join(f"{i['quantity']} x {i['product']} ({i['variant']}) @ {money(i['unit_price'])}" for i in items)
            notify(f"New order #{1000 + order_id} — {money(totals['total'])}",
                   f"{form['name']} / {form['phone']} / {form['email']}\n"
                   f"{form['address']}, {form['city']}, {form['state']} {form['pincode']}\n"
                   f"Payment: {method['label']}\n\n{summary}\n\nTotal: {money(totals['total'])}\n"
                   f"Notes: {form['notes'] or '-'}")
            session["cart"] = {}
            session["last_order"] = order_id
            return redirect(url_for("thanks", order_id=order_id))

    return render_template("checkout.html", lines=lines, totals=totals, methods=methods,
                           form=form, errors=errors, page_title="Checkout")


@app.route("/order/<int:order_id>/thanks")
def thanks(order_id):
    if session.get("last_order") != order_id:   # only the buyer can see their confirmation
        abort(404)
    row = db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("thanks.html", order=row, items=json.loads(row["items_json"]),
                           order_number=1000 + order_id, page_title="Order confirmed")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    cfg = load_json("site.json")["contact"]
    if request.method == "POST":
        if request.form.get("website"):          # honeypot
            return redirect(url_for("contact", sent=1))
        name = request.form.get("name", "").strip()[:100]
        email = request.form.get("email", "").strip()[:200]
        topic = request.form.get("topic", "Other")
        body = request.form.get("body", "").strip()[:5000]
        if topic not in cfg["topics"]:
            topic = "Other"
        if not name or not EMAIL_RE.match(email) or not body:
            flash("Please fill in your name, a valid email and a message.", "error")
            return render_template("contact.html", cfg=cfg, form=request.form, page_title="Contact"), 400
        db().execute("INSERT INTO messages (name,email,topic,body) VALUES (?,?,?,?)",
                     (name, email, topic, body))
        db().commit()
        notify(f"Contact form: {topic} — {name}", f"From: {name} <{email}>\nTopic: {topic}\n\n{body}")
        return redirect(url_for("contact", sent=1))
    return render_template("contact.html", cfg=cfg, form={}, sent=request.args.get("sent"),
                           page_title="Contact")


# --------------------------------------------------------------------------
# Admin (set ADMIN_PASSWORD to enable; username is "admin")
# --------------------------------------------------------------------------
def admin_ok():
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        abort(404)
    auth = request.authorization
    good = (auth and auth.username == "admin"
            and hmac.compare_digest((auth.password or "").encode(), pw.encode()))
    if not good:
        abort(Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'}))


@app.route("/admin")
def admin():
    admin_ok()
    orders = db().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    orders = [dict(o, items=json.loads(o["items_json"]), number=1000 + o["id"]) for o in orders]
    messages = db().execute("SELECT * FROM messages ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("admin.html", orders=orders, messages=messages,
                           statuses=ORDER_STATUSES, page_title="Admin")


@app.post("/admin/order/<int:order_id>/status")
def admin_status(order_id):
    admin_ok()
    status = request.form.get("status")
    if status in ORDER_STATUSES:
        db().execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        db().commit()
    return redirect(url_for("admin"))


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /cart\nDisallow: /checkout\n", mimetype="text/plain")


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="We couldn’t find that page.", page_title="Not found"), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request."), page_title="Error"), 400


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
