"""Thread & Co. — standalone storefront (Flask).

Catalogue, cart, discount codes, stock counts, checkout (Razorpay online
payments + Cash on Delivery), order emails, order tracking, contact form,
search, SEO files and a password-protected admin.

Content lives in data/*.json. Orders, messages and stock live in SQLite.
"""
import base64
import copy
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import tempfile
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, has_app_context, redirect,
                   render_template, request, send_from_directory, session, url_for)
from markupsafe import Markup

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "store.db"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))
CONTENT_DOCS = ["site.json", "products.json", "discounts.json", "pages.json"]
PER_PAGE = 12
MAX_QTY = 20
PENDING_MINUTES = 30          # how long stock is held for an unpaid online order
ORDER_STATUSES = ["pending_payment", "new", "confirmed", "shipped", "delivered", "cancelled"]
STATUS_LABELS = {"pending_payment": "Awaiting payment", "new": "Order received",
                 "confirmed": "Confirmed", "shipped": "Shipped",
                 "delivered": "Delivered", "cancelled": "Cancelled"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=24 * 1024 * 1024,      # admin image uploads
)
log = logging.getLogger("threadandco")
if os.environ.get("TRUST_PROXY") == "1":       # running behind Render/nginx/etc: trust X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# --------------------------------------------------------------------------
# Content (site settings, products, discounts, pages) lives in the database so
# the admin page can edit it. data/*.json only provides the starting content.
# --------------------------------------------------------------------------
_cache = {}


def content_versions():
    if "cv" not in g:
        g.cv = {r["name"]: r["version"] for r in db().execute("SELECT name, version FROM content")}
    return g.cv


def load_json(name, default=None):
    """Read-only view of a content document (cached per process; do NOT mutate)."""
    ver = content_versions().get(name)
    if ver is None:
        if default is not None:
            return default
        raise KeyError(name)
    hit = _cache.get(name)
    if not hit or hit[0] != ver:
        row = db().execute("SELECT json FROM content WHERE name=?", (name,)).fetchone()
        _cache[name] = (ver, json.loads(row["json"]))
    return _cache[name][1]


def read_doc(name, default=None):
    """A fresh, private copy of a document — use this before editing."""
    row = db().execute("SELECT json FROM content WHERE name=?", (name,)).fetchone()
    if not row:
        return copy.deepcopy(default) if default is not None else None
    return json.loads(row["json"])


def write_doc(name, data):
    conn = db()
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    cur = conn.execute("UPDATE content SET json=?, version=version+1, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                       (payload, name))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO content (name, json) VALUES (?, ?)", (name, payload))
    conn.commit()
    g.pop("cv", None)


def catalog():
    """Visible products with derived fields (live stock is added at render time)."""
    ver = content_versions().get("products.json")
    hit = _cache.get("_catalog")
    if hit and hit[0] == ver:
        return hit[1]
    raw = copy.deepcopy(load_json("products.json"))
    products = [p for p in raw["products"] if not p.get("hidden")]
    by_handle, variants = {}, {}
    for p in products:
        vs = p["variants"]
        p["price"] = min(v["price"] for v in vs)
        p["compare_at_max"] = max((v.get("compare_at_price") or 0) for v in vs)
        p["has_only_default_variant"] = len(vs) == 1 and vs[0]["title"] == "Default Title"
        p["featured_image"] = p["images"][0] if p.get("images") else None
        p["url"] = f"/products/{p['handle']}"
        by_handle[p["handle"]] = p
        for v in vs:
            variants[v["id"]] = (p, v)
    cat = {"collections": {c["handle"]: c for c in raw["collections"]},
           "products": products, "by_handle": by_handle, "variants": variants}
    _cache["_catalog"] = (ver, cat)
    return cat


def collection_products(handle):
    cat = catalog()
    if handle == "all":
        return cat["products"]
    return [p for p in cat["products"] if handle in p.get("collections", [])]


def sort_products(items, sort):
    if sort == "price-asc":
        return sorted(items, key=lambda p: p["price"])
    if sort == "price-desc":
        return sorted(items, key=lambda p: -p["price"])
    if sort == "title":
        return sorted(items, key=lambda p: p["title"].lower())
    return items


SORT_OPTIONS = [("", "Featured"), ("price-asc", "Price: low to high"),
                ("price-desc", "Price: high to low"), ("title", "Name: A–Z")]


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


def abs_url(path):
    base = os.environ.get("SITE_URL") or request.url_root
    if path.startswith(("http://", "https://")):
        return path
    return base.rstrip("/") + path


def plain(html):
    return Markup(html or "").striptags()


app.jinja_env.filters["money"] = money
app.jinja_env.filters["img_url"] = img_url
app.jinja_env.filters["nl2br"] = lambda s: re.sub(r"\n", "<br>", s)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


ORDER_EXTRA_COLUMNS = {
    "payment_status": "TEXT DEFAULT 'cod'", "razorpay_order_id": "TEXT",
    "razorpay_payment_id": "TEXT", "discount_code": "TEXT",
    "discount_amount": "INTEGER DEFAULT 0", "cod_fee": "INTEGER DEFAULT 0",
    "tracking": "TEXT", "admin_note": "TEXT",
}


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
        CREATE TABLE IF NOT EXISTS inventory (
            variant_id TEXT PRIMARY KEY,
            stock INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS content (
            name TEXT PRIMARY KEY,
            json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    for col, decl in ORDER_EXTRA_COLUMNS.items():       # upgrades older databases in place
        if col not in have:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    for name in CONTENT_DOCS:                           # first run: copy starting content from data/*.json
        if not conn.execute("SELECT 1 FROM content WHERE name=?", (name,)).fetchone() and (DATA_DIR / name).exists():
            conn.execute("INSERT INTO content (name, json) VALUES (?, ?)",
                         (name, (DATA_DIR / name).read_text(encoding="utf-8")))
    conn.commit()
    conn.close()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


init_db()


# --------------------------------------------------------------------------
# Inventory (variants with a "stock" number in products.json are counted)
# --------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc)


def restock(conn, items):
    for it in items:
        conn.execute("UPDATE inventory SET stock = stock + ? WHERE variant_id=?",
                     (it["quantity"], it["variant_id"]))


def expire_pending():
    """Release stock held by online orders that were never paid."""
    cutoff = (utcnow() - timedelta(minutes=PENDING_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    rows = conn.execute("SELECT id, items_json FROM orders WHERE status='pending_payment' "
                        "AND created_at < ?", (cutoff,)).fetchall()
    for r in rows:
        restock(conn, json.loads(r["items_json"]))
        conn.execute("UPDATE orders SET status='cancelled', payment_status='expired' WHERE id=?", (r["id"],))
    if rows:
        conn.commit()


def inventory():
    """{variant_id: stock} for counted variants, loaded once per request."""
    if "inv" not in g:
        expire_pending()
        conn = db()
        rows = {r["variant_id"]: r["stock"] for r in conn.execute("SELECT variant_id, stock FROM inventory")}
        missing = [(v["id"], v["stock"]) for p in load_json("products.json")["products"]
                   for v in p["variants"] if "stock" in v and v["id"] not in rows]
        if missing:
            conn.executemany("INSERT OR IGNORE INTO inventory VALUES (?,?)", missing)
            conn.commit()
            rows.update(dict(missing))
        g.inv = rows
    return g.inv


def stock_of(variant):
    """Units left, or None if this variant isn't counted."""
    if "stock" not in variant:
        return None
    return inventory().get(variant["id"], variant["stock"])


def is_available(variant):
    if not variant.get("available", True):
        return False
    s = stock_of(variant)
    return s is None or s > 0


def product_available(product):
    return any(is_available(v) for v in product["variants"])


def reserve(conn, items):
    """Atomically take stock for an order. Returns an error message or None."""
    inventory()                                   # makes sure rows exist
    variants = catalog()["variants"]
    for it in items:
        found = variants.get(it["variant_id"])
        if not found or "stock" not in found[1]:
            continue
        cur = conn.execute("UPDATE inventory SET stock = stock - ? WHERE variant_id=? AND stock >= ?",
                           (it["quantity"], it["variant_id"], it["quantity"]))
        if cur.rowcount == 0:
            conn.rollback()
            left = conn.execute("SELECT stock FROM inventory WHERE variant_id=?", (it["variant_id"],)).fetchone()
            left = left["stock"] if left else 0
            name = f"{it['product']} ({it['variant']})" if it["variant"] != "Default Title" else it["product"]
            return (f"Sorry, {name} just sold out." if left <= 0
                    else f"Sorry, only {left} left of {name}. Please update your bag.")
    g.pop("inv", None)
    return None


app.jinja_env.globals.update(avail=is_available, stock_of=stock_of,
                             product_available=product_available, abs_url=abs_url,
                             status_labels=STATUS_LABELS)


# --------------------------------------------------------------------------
# Cart, discounts, totals
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
                      "line_price": variant["price"] * qty,
                      "stock": stock_of(variant), "available": is_available(variant)})
    return lines


def load_discounts():
    return load_json("discounts.json", default={})


def check_discount(code, subtotal, email=None):
    """-> (discount dict or None, error message or None)"""
    code = (code or "").strip().upper()
    d = load_discounts().get(code)
    if not d or not d.get("active", True):
        return None, "That code isn’t valid."
    if d.get("expires") and date.today() > date.fromisoformat(d["expires"]):
        return None, "That code has expired."
    if subtotal < d.get("min_order", 0):
        return None, f"This code needs a minimum order of {money(d['min_order'])}."
    conn = db()
    if d.get("max_uses") is not None:
        used = conn.execute("SELECT COUNT(*) FROM orders WHERE discount_code=? AND status!='cancelled'",
                            (code,)).fetchone()[0]
        if used >= d["max_uses"]:
            return None, "That code has been fully redeemed."
    if d.get("once_per_customer") and email:
        used = conn.execute("SELECT COUNT(*) FROM orders WHERE discount_code=? AND status!='cancelled' "
                            "AND lower(email)=lower(?)", (code, email)).fetchone()[0]
        if used:
            return None, "You’ve already used this code."
    return dict(d, code=code), None


def cart_totals(lines, payment_id=None, email=None):
    cfg = load_json("site.json")["shipping"]
    subtotal = sum(l["line_price"] for l in lines)
    discount, code, err = 0, None, None
    entered = session.get("discount")
    if entered and lines:
        d, err = check_discount(entered, subtotal, email)
        if d:
            code = d["code"]
            if d["type"] == "percent":
                discount = round(subtotal * d["value"] / 100)
            elif d["type"] == "flat":
                discount = d["value"]
            discount = min(discount, subtotal)
        else:
            session.pop("discount", None)
    shipping = 0 if (not lines or subtotal >= cfg["free_over"]) else cfg["flat_rate"]
    if code and load_discounts()[code]["type"] == "free_shipping":
        shipping = 0
    cod_fee = cfg.get("cod_fee", 0) if (payment_id == "cod" and lines) else 0
    return {"subtotal": subtotal, "discount": discount, "code": code, "discount_error": err,
            "shipping": shipping, "cod_fee": cod_fee,
            "total": subtotal - discount + shipping + cod_fee,
            "count": sum(l["quantity"] for l in lines), "free_over": cfg["free_over"]}


@app.context_processor
def inject_globals():
    return {"site": load_json("site.json"),
            "cart_count": sum(session.get("cart", {}).values()),
            "csrf_token": csrf_token, "now_year": datetime.now().year,
            "sort_options": SORT_OPTIONS}


@app.before_request
def csrf_protect():
    if request.method == "POST" and request.endpoint != "razorpay_webhook":
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
# Email (sent in the background; only if SMTP_* env vars are set)
# --------------------------------------------------------------------------
def send_email(to, subject, body, reply_to=None):
    host = os.environ.get("SMTP_HOST")
    if not host or not to:
        log.info("(email not sent — SMTP not configured) to=%s subject=%s", to, subject)
        return
    sender = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or to
    port = int(os.environ.get("SMTP_PORT", 587))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD", "")

    def _send():
        try:
            msg = EmailMessage()
            msg["Subject"], msg["To"], msg["From"] = subject, to, sender
            if reply_to:
                msg["Reply-To"] = reply_to
            msg.set_content(body)
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                if user:
                    s.login(user, password)
                s.send_message(msg)
        except Exception:                       # an email problem must never break an order
            log.exception("Could not send email to %s", to)

    threading.Thread(target=_send, daemon=True).start()


def owner_email():
    return os.environ.get("NOTIFY_EMAIL") or load_json("site.json")["contact_email"]


def notify(subject, body, reply_to=None):
    send_email(owner_email(), subject, body, reply_to)


def item_lines(items):
    out = []
    for i in items:
        label = f" ({i['variant']})" if i["variant"] != "Default Title" else ""
        out.append(f"  {i['quantity']} x {i['product']}{label} — {money(i['unit_price'] * i['quantity'])}")
    return "\n".join(out)


def order_money_lines(row):
    out = [f"  Subtotal: {money(row['subtotal'])}"]
    if row["discount_amount"]:
        out.append(f"  Discount ({row['discount_code']}): -{money(row['discount_amount'])}")
    out.append(f"  Shipping: {'Free' if not row['shipping'] else money(row['shipping'])}")
    if row["cod_fee"]:
        out.append(f"  COD fee: {money(row['cod_fee'])}")
    out.append(f"  Total: {money(row['total'])}")
    return "\n".join(out)


def send_order_emails(order_id):
    row = db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return
    shop = load_json("site.json")["shop_name"]
    items, num = json.loads(row["items_json"]), 1000 + row["id"]
    paid = "Paid online" if row["payment_status"] == "paid" else row["payment_method"]
    send_email(row["email"], f"Your {shop} order #{num}",
               f"Hi {row['name']},\n\nThanks for your order! Here’s your summary.\n\n"
               f"Order #{num}\n{item_lines(items)}\n\n{order_money_lines(row)}\n\n"
               f"Payment: {paid}\n"
               f"Delivering to: {row['address']}, {row['city']}, {row['state']} {row['pincode']}\n\n"
               f"We’ll email you again when it ships. You can check progress any time at "
               f"{abs_url('/track')} using your order number and this email address.\n\n— {shop}",
               reply_to=owner_email())
    notify(f"New order #{num} — {money(row['total'])} ({paid})",
           f"{row['name']} / {row['phone']} / {row['email']}\n"
           f"{row['address']}, {row['city']}, {row['state']} {row['pincode']}\n"
           f"Payment: {paid}\n\n{item_lines(items)}\n\n{order_money_lines(row)}\n\n"
           f"Notes: {row['notes'] or '-'}", reply_to=row["email"])


def send_status_email(row, status):
    shop = load_json("site.json")["shop_name"]
    num = 1000 + row["id"]
    if status == "shipped":
        extra = f"\nTracking details: {row['tracking']}\n" if row["tracking"] else ""
        body = (f"Hi {row['name']},\n\nGood news — your order #{num} has shipped.\n{extra}\n"
                f"Check status any time: {abs_url('/track')}\n\n— {shop}")
        send_email(row["email"], f"Your {shop} order #{num} has shipped", body, reply_to=owner_email())
    elif status == "cancelled":
        refund = ("\nSince you paid online, your refund will be sent to your original payment method.\n"
                  if row["payment_status"] == "paid" else "")
        send_email(row["email"], f"Your {shop} order #{num} was cancelled",
                   f"Hi {row['name']},\n\nYour order #{num} has been cancelled.\n{refund}\n"
                   f"If you have questions, just reply to this email.\n\n— {shop}", reply_to=owner_email())


# --------------------------------------------------------------------------
# Razorpay (online payments). Needs RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET.
# --------------------------------------------------------------------------
def rz_keys():
    return os.environ.get("RAZORPAY_KEY_ID"), os.environ.get("RAZORPAY_KEY_SECRET")


def razorpay_enabled():
    return all(rz_keys())


def razorpay_create_order(amount_rupees, receipt, notes):
    key, secret = rz_keys()
    payload = json.dumps({"amount": int(amount_rupees) * 100, "currency": "INR",
                          "receipt": receipt, "notes": notes}).encode()
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    req = urllib.request.Request("https://api.razorpay.com/v1/orders", data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def razorpay_signature_ok(rz_order_id, payment_id, signature):
    _, secret = rz_keys()
    if not secret or not rz_order_id or not payment_id or not signature:
        return False
    expected = hmac.new(secret.encode(), f"{rz_order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def mark_paid(order_id, payment_id):
    """Idempotent. Returns True only for the call that actually flipped it to paid."""
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row["payment_status"] == "paid":
        return False
    note = row["admin_note"] or ""
    revive = row["status"] == "cancelled" and row["payment_status"] == "expired"
    if revive:      # the stock hold had lapsed; take stock again (never below zero)
        for it in json.loads(row["items_json"]):
            conn.execute("UPDATE inventory SET stock = MAX(stock - ?, 0) WHERE variant_id=?",
                         (it["quantity"], it["variant_id"]))
        note = (note + " Paid after the stock hold expired — please check stock.").strip()
    elif row["status"] == "cancelled":
        note = (note + " Payment arrived after you cancelled this order — refund needed.").strip()
    cur = conn.execute(
        """UPDATE orders SET payment_status='paid', razorpay_payment_id=?, admin_note=?,
           status=CASE WHEN status='pending_payment' OR ?=1 THEN 'new' ELSE status END
           WHERE id=? AND payment_status!='paid'""", (payment_id, note, 1 if revive else 0, order_id))
    conn.commit()
    g.pop("inv", None)
    return cur.rowcount == 1


def own_order(order_id):
    """Only the browser that placed the order may open its payment/confirmation page."""
    if session.get("last_order") != order_id:
        abort(404)
    row = db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        abort(404)
    return row


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
                           featured_collection=col, products=products, page_title="Home",
                           og_image=site.get("og_image"))


@app.route("/collections/<handle>")
def collection(handle):
    col = catalog()["collections"].get(handle)
    if not col:
        abort(404)
    sort = request.args.get("sort") if request.args.get("sort") in {o[0] for o in SORT_OPTIONS if o[0]} else None
    items = sort_products(collection_products(handle), sort)
    pages = max(1, -(-len(items) // PER_PAGE))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    items = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    return render_template("collection.html", collection=col, products=items, page=page,
                           pages=pages, sort=sort, page_title=col["title"],
                           meta_description=col.get("description") or None)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()[:80]
    results = []
    if q:
        words = q.lower().split()
        for p in catalog()["products"]:
            hay = f"{p['title']} {p['vendor']} {plain(p['description'])}".lower()
            if all(w in hay for w in words):
                results.append(p)
    return render_template("search.html", q=q, products=results, page_title="Search", noindex=True)


@app.route("/products/<handle>")
def product(handle):
    p = catalog()["by_handle"].get(handle)
    if not p:
        abort(404)
    first = next((v for v in p["variants"] if is_available(v)), p["variants"][0])
    ld = {"@context": "https://schema.org", "@type": "Product", "name": p["title"],
          "description": str(plain(p["description"])), "brand": {"@type": "Brand", "name": p["vendor"]},
          "image": [abs_url(img_url(i)) for i in p.get("images", [])],
          "offers": {"@type": "Offer", "priceCurrency": "INR", "price": p["price"],
                     "url": abs_url(p["url"]),
                     "availability": "https://schema.org/InStock" if product_available(p)
                     else "https://schema.org/OutOfStock"}}
    return render_template("product.html", product=p, selected=first, page_title=p["title"],
                           meta_description=str(plain(p["description"]))[:160],
                           og_image=p["featured_image"], ld=ld)


@app.route("/pages/<handle>")
def page(handle):
    pg = load_json("pages.json").get(handle)
    if not pg:
        abort(404)
    return render_template("page.html", page=pg, page_title=pg["title"],
                           meta_description=str(plain(pg["content"]))[:160])


@app.route("/cart")
def cart():
    lines = cart_lines()
    totals = cart_totals(lines)
    if totals["discount_error"]:
        flash(totals["discount_error"], "error")
    return render_template("cart.html", lines=lines, totals=totals, page_title="Your bag", noindex=True)


@app.post("/cart/add")
def cart_add():
    vid = request.form.get("id", "")
    found = catalog()["variants"].get(vid)
    if not found:
        abort(404)
    _, variant = found
    back = request.referrer or url_for("cart")
    if not is_available(variant):
        flash("Sorry, that option is sold out.", "error")
        return redirect(back)
    qty = max(1, min(request.form.get("quantity", 1, type=int) or 1, MAX_QTY))
    cart_data = dict(session.get("cart", {}))
    have = cart_data.get(vid, 0)
    new = min(have + qty, MAX_QTY)
    left = stock_of(variant)
    if left is not None:
        new = min(new, left)
    if new <= have:
        flash("You already have all the available stock for that item in your bag.", "error")
        return redirect(url_for("cart"))
    if new < have + qty and left is not None and new == left:
        flash(f"Only {left} available — we’ve added what we have.", "error")
    cart_data[vid] = new
    session["cart"] = cart_data
    return redirect(url_for("cart"))


@app.post("/cart/update")
def cart_update():
    cart_data = {}
    variants = catalog()["variants"]
    for vid in session.get("cart", {}):
        raw = request.form.get(f"qty_{vid}")
        qty = int(raw) if raw and raw.isdigit() else session["cart"][vid]
        if vid in variants:
            left = stock_of(variants[vid][1])
            if left is not None and qty > left:
                qty = left
                flash(f"Only {left} left of {variants[vid][0]['title']} — quantity adjusted.", "error")
        if qty > 0:
            cart_data[vid] = min(qty, MAX_QTY)
    session["cart"] = cart_data
    if "checkout" in request.form and cart_data:
        return redirect(url_for("checkout"))
    return redirect(url_for("cart"))


@app.post("/cart/discount")
def cart_discount():
    if request.form.get("remove"):
        session.pop("discount", None)
        return redirect(url_for("cart"))
    code = request.form.get("code", "").strip().upper()[:40]
    lines = cart_lines()
    subtotal = sum(l["line_price"] for l in lines)
    d, err = check_discount(code, subtotal)
    if d:
        session["discount"] = code
        flash(f"Code {code} applied.")
    else:
        flash(err, "error")
    return redirect(url_for("cart"))


def payment_methods():
    return [m for m in load_json("site.json")["payment_methods"]
            if m["id"] != "razorpay" or razorpay_enabled()]


PHONE_RE = re.compile(r"^(\+91[\s-]?)?[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    lines = cart_lines()
    if not lines:
        return redirect(url_for("cart"))
    methods = payment_methods()
    form, errors = {}, {}

    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ["name", "email", "phone", "address", "city", "state", "pincode", "notes", "payment"]}
        if request.form.get("website"):                  # honeypot: bots fill this in
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
        had_code = bool(session.get("discount"))
        totals = cart_totals(lines, form["payment"], form["email"])
        if had_code and not totals["code"]:
            errors["cart"] = totals["discount_error"] or "Your discount code is no longer valid."
        for l in lines:
            name = l["product"]["title"] + ("" if l["variant"]["title"] == "Default Title" else f" ({l['variant']['title']})")
            if not l["available"]:
                errors["cart"] = f"{name} just sold out. Please update your bag."
            elif l["stock"] is not None and l["quantity"] > l["stock"]:
                errors["cart"] = f"Only {l['stock']} left of {name}. Please update your bag."

        if not errors:
            items = [{"product": l["product"]["title"], "variant": l["variant"]["title"],
                      "variant_id": l["variant"]["id"], "quantity": l["quantity"],
                      "unit_price": l["variant"]["price"]} for l in lines]
            conn = db()
            problem = reserve(conn, items)
            if problem:
                errors["cart"] = problem
            else:
                online = method["id"] == "razorpay"
                cur = conn.execute(
                    """INSERT INTO orders (name,email,phone,address,city,state,pincode,notes,
                       payment_method,payment_status,status,discount_code,discount_amount,cod_fee,
                       items_json,subtotal,shipping,total)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (form["name"], form["email"], form["phone"], form["address"], form["city"],
                     form["state"], form["pincode"], form["notes"], method["label"],
                     "pending" if online else "cod", "pending_payment" if online else "new",
                     totals["code"], totals["discount"], totals["cod_fee"],
                     json.dumps(items), totals["subtotal"], totals["shipping"], totals["total"]))
                conn.commit()
                order_id = cur.lastrowid
                if online:
                    try:
                        rz = razorpay_create_order(totals["total"], f"TC-{1000 + order_id}",
                                                   {"order_number": str(1000 + order_id)})
                        conn.execute("UPDATE orders SET razorpay_order_id=? WHERE id=?", (rz["id"], order_id))
                        conn.commit()
                    except Exception:
                        log.exception("Razorpay order creation failed")
                        restock(conn, items)
                        conn.execute("UPDATE orders SET status='cancelled', payment_status='failed' WHERE id=?",
                                     (order_id,))
                        conn.commit()
                        g.pop("inv", None)
                        errors["payment"] = ("Online payment isn’t available right now. "
                                             "Please try again, or choose Cash on Delivery.")
                if not errors:
                    session["cart"] = {}
                    session.pop("discount", None)
                    session["last_order"] = order_id
                    if online:
                        return redirect(url_for("pay", order_id=order_id))
                    send_order_emails(order_id)
                    return redirect(url_for("thanks", order_id=order_id))
        lines = cart_lines()      # refresh stock figures before re-showing the form
    else:
        totals = cart_totals(lines, methods[0]["id"])
        if totals["discount_error"]:
            flash(totals["discount_error"], "error")
    shown = cart_totals(lines, form.get("payment") or methods[0]["id"], form.get("email"))
    return render_template("checkout.html", lines=lines, totals=shown, methods=methods, form=form,
                           errors=errors, cod_fee=load_json("site.json")["shipping"].get("cod_fee", 0),
                           page_title="Checkout", noindex=True)


@app.route("/pay/<int:order_id>")
def pay(order_id):
    row = own_order(order_id)
    if row["payment_status"] == "paid":
        return redirect(url_for("thanks", order_id=order_id))
    if row["status"] != "pending_payment" or not row["razorpay_order_id"]:
        return render_template("error.html", code="PAYMENT",
                               message="This payment window has expired. Please place your order again.",
                               page_title="Payment expired"), 410
    return render_template("pay.html", order=row, number=1000 + order_id, key_id=rz_keys()[0],
                           page_title="Pay", noindex=True)


@app.post("/pay/<int:order_id>/verify")
def pay_verify(order_id):
    row = own_order(order_id)
    rz_order = request.form.get("razorpay_order_id", "")
    payment_id = request.form.get("razorpay_payment_id", "")
    sig = request.form.get("razorpay_signature", "")
    if rz_order != row["razorpay_order_id"] or not razorpay_signature_ok(rz_order, payment_id, sig):
        flash("We couldn’t verify that payment. If money was deducted, it will be matched "
              "automatically or refunded — please contact us with your order number.", "error")
        return redirect(url_for("pay", order_id=order_id))
    if mark_paid(order_id, payment_id):
        send_order_emails(order_id)
    return redirect(url_for("thanks", order_id=order_id))


@app.post("/razorpay/webhook")
def razorpay_webhook():
    """Backup path: confirms payment even if the customer closes the browser tab."""
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        abort(404)
    body = request.get_data()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Razorpay-Signature", "")):
        abort(400)
    event = json.loads(body or b"{}")
    if event.get("event") in ("payment.captured", "order.paid"):
        pay_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        row = db().execute("SELECT id FROM orders WHERE razorpay_order_id=?",
                           (pay_entity.get("order_id"),)).fetchone()
        if row and mark_paid(row["id"], pay_entity.get("id")):
            send_order_emails(row["id"])
    return "ok"


@app.route("/order/<int:order_id>/thanks")
def thanks(order_id):
    row = own_order(order_id)
    if row["status"] == "pending_payment":
        return redirect(url_for("pay", order_id=order_id))
    return render_template("thanks.html", order=row, items=json.loads(row["items_json"]),
                           order_number=1000 + order_id, page_title="Order confirmed", noindex=True)


@app.route("/track", methods=["GET", "POST"])
def track():
    order = items = None
    error = None
    if request.method == "POST":
        num = re.sub(r"\D", "", request.form.get("number", ""))
        email = request.form.get("email", "").strip().lower()
        row = db().execute("SELECT * FROM orders WHERE id=?", (int(num) - 1000,)).fetchone() if num and int(num) > 1000 else None
        if row and row["email"].lower() == email and row["status"] != "pending_payment":
            order, items = row, json.loads(row["items_json"])
        else:
            error = "We couldn’t find an order with that number and email."
    return render_template("track.html", order=order, items=items, error=error,
                           steps=["new", "confirmed", "shipped", "delivered"], page_title="Track order", noindex=True)


@app.route("/contact", methods=["GET", "POST"])
def contact():
    cfg = load_json("site.json")["contact"]
    if request.method == "POST":
        if request.form.get("website"):                  # honeypot
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
        db().execute("INSERT INTO messages (name,email,topic,body) VALUES (?,?,?,?)", (name, email, topic, body))
        db().commit()
        notify(f"Contact form: {topic} — {name}", f"From: {name} <{email}>\nTopic: {topic}\n\n{body}", reply_to=email)
        return redirect(url_for("contact", sent=1))
    return render_template("contact.html", cfg=cfg, form={}, sent=request.args.get("sent"), page_title="Contact")


# --------------------------------------------------------------------------
# Admin access (set ADMIN_PASSWORD to enable; username is "admin"). Pages: admin.py
# --------------------------------------------------------------------------
_failed_logins = {}


def admin_ok():
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        abort(404)
    ip, now = request.remote_addr or "?", utcnow().timestamp()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < 900]
    if len(recent) >= 10:
        abort(Response("Too many failed logins. Try again in 15 minutes.", 429))
    auth = request.authorization
    good = (auth and auth.username == "admin"
            and hmac.compare_digest((auth.password or "").encode(), pw.encode()))
    if not good:
        if auth:
            _failed_logins[ip] = recent + [now]
        abort(Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'}))
    _failed_logins.pop(ip, None)


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=31536000)


# --------------------------------------------------------------------------
# Utility routes: health, robots, sitemap, errors
# --------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /cart\nDisallow: /checkout\n"
                    "Disallow: /pay\nDisallow: /track\nDisallow: /search\n"
                    f"Sitemap: {abs_url('/sitemap.xml')}\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    cat = catalog()
    paths = ["/", "/contact"] + [f"/collections/{h}" for h in cat["collections"]] + \
            [p["url"] for p in cat["products"]] + [f"/pages/{h}" for h in load_json("pages.json")]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "".join(f"  <url><loc>{escape(abs_url(p))}</loc></url>\n" for p in paths) + "</urlset>\n")
    return Response(xml, mimetype="application/xml")


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="We couldn’t find that page.", page_title="Not found"), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request."), page_title="Error"), 400


import sys as _sys                       # noqa: E402  (admin.py gets this module; no circular import)
import admin as _admin                   # noqa: E402
_admin.init(_sys.modules[__name__])


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
