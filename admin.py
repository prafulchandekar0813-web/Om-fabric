"""Admin panel (Flask blueprint) — manage the whole store from the browser.

Registered from app.py via init(core). Everything here requires the admin login.
"""
import csv
import io
import json
import re
import secrets
import sqlite3
import tempfile
import os
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template,
                   request, url_for)
from PIL import Image, ImageOps

core = None
bp = Blueprint("adm", __name__, url_prefix="/admin")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def init(c):
    global core
    core = c
    Image.MAX_IMAGE_PIXELS = 40_000_000          # refuse "decompression bomb" images
    c.app.register_blueprint(bp)


@bp.before_request
def _require_login():
    core.admin_ok()


def db():
    return core.db()


def render(template, section, **ctx):
    return render_template(template, section=section, page_title="Admin", noindex=True, **ctx)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def slugify(text, maxlen=60):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:maxlen].strip("-")


def parse_money(raw):
    raw = re.sub(r"[₹,\s]", "", raw or "")
    return int(float(raw)) if re.fullmatch(r"\d{1,7}(\.0+)?", raw) else None


def parse_int(raw, lo=0, hi=10**7):
    raw = (raw or "").strip()
    if not re.fullmatch(r"\d{1,9}", raw):
        return None
    n = int(raw)
    return n if lo <= n <= hi else None


_ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a", "h2", "h3", "h4",
                 "blockquote", "hr"}
_VOID = {"br", "hr"}


class _Cleaner(HTMLParser):
    """Keeps a small safe set of tags (no scripts, no event handlers, no javascript: links)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag in _VOID:
            self.out.append(f"<{tag}>")
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if re.match(r"^(https?://|mailto:|/|#)", href, re.I):
                self.out.append(f'<a href="{escape(href, quote=True)}" rel="noopener">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data))

    def finish(self):
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def to_html(text):
    """Plain text -> paragraphs; HTML -> sanitised HTML."""
    text = (text or "").strip()
    if not text:
        return ""
    if "<" not in text:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        text = "".join("<p>" + escape(p).replace("\n", "<br>") + "</p>" for p in paras)
    c = _Cleaner()
    c.feed(text)
    return c.finish()


def save_image(fs):
    """Validate + re-encode an uploaded image (strips metadata, max 1600px). Returns '/uploads/x.webp'."""
    data = fs.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{fs.filename}: larger than 8 MB.")
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        im = Image.open(io.BytesIO(data))
        if im.format not in ("JPEG", "PNG", "WEBP", "GIF"):
            raise ValueError
        im = ImageOps.exif_transpose(im)
        im.thumbnail((1600, 1600))
        if im.mode != "RGB":
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P", "PA") else "RGB")
    except Exception:
        raise ValueError(f"{fs.filename}: not a valid image (use JPG, PNG, WebP or GIF).")
    name = secrets.token_hex(8) + ".webp"
    core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    im.save(core.UPLOAD_DIR / name, "WEBP", quality=85)
    return "/uploads/" + name


def delete_upload_if_unused(path):
    if not path or not path.startswith("/uploads/"):
        return
    blob = " ".join(json.dumps(core.read_doc(n, {}), ensure_ascii=False) for n in core.CONTENT_DOCS)
    if path in blob:
        return
    try:
        (core.UPLOAD_DIR / Path(path).name).unlink()
    except FileNotFoundError:
        pass


def lines_to_menu(text, label, errors):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        title, _, url = [x.strip() for x in line.partition("|")]
        if not title or not re.match(r"^(/|https?://|mailto:)", url):
            errors.append(f"{label}: write each line as “Title | /link” (problem: {line[:40]}).")
            continue
        out.append({"title": title[:40], "url": url[:300]})
    return out


def menu_to_lines(menu):
    return "\n".join(f"{m['title']} | {m['url']}" for m in menu)


# --------------------------------------------------------------------------
# Orders + dashboard
# --------------------------------------------------------------------------
@bp.route("/", strict_slashes=False)
def orders():
    core.inventory()                                        # also releases expired payment holds
    conn = db()
    status = request.args.get("status")
    if status in core.ORDER_STATUSES:
        rows = conn.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 300", (status,)).fetchall()
    else:
        status = None
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 300").fetchall()
    rows = [dict(o, items=json.loads(o["items_json"]), number=1000 + o["id"]) for o in rows]
    counts = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status")}
    revenue = conn.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status NOT IN ('cancelled','pending_payment')").fetchone()[0]
    today = conn.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now') AND status!='cancelled'").fetchone()[0]
    low = [(p, v, core.stock_of(v)) for p in core.load_json("products.json")["products"]
           for v in p["variants"] if "stock" in v and core.stock_of(v) is not None and core.stock_of(v) <= 3]
    return render("admin_orders.html", "orders", orders=rows, statuses=core.ORDER_STATUSES, status=status,
                  counts=counts, revenue=revenue, today=today, low=low,
                  razorpay=core.razorpay_enabled(), smtp=bool(os.environ.get("SMTP_HOST")))


@bp.post("/order/<int:order_id>/status")
def order_status(order_id):
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        abort(404)
    status = request.form.get("status")
    tracking = request.form.get("tracking", "").strip()[:200]
    conn.execute("UPDATE orders SET tracking=? WHERE id=?", (tracking, order_id))
    if status in core.ORDER_STATUSES and status != row["status"]:
        if row["status"] == "cancelled":
            flash("Cancelled orders can’t be reopened — ask the customer to place a new order.", "error")
        else:
            if status == "cancelled":
                core.restock(conn, json.loads(row["items_json"]))
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
            g.pop("inv", None)
            conn.commit()
            fresh = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if status in ("shipped", "cancelled"):
                core.send_status_email(fresh, status)
    conn.commit()
    return redirect(request.referrer or url_for("adm.orders"))


def _csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@bp.route("/orders.csv")
def orders_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["order", "placed_utc", "status", "payment_status", "payment_method", "name", "email",
                "phone", "address", "city", "state", "pincode", "items", "subtotal", "discount_code",
                "discount", "shipping", "cod_fee", "total", "tracking", "notes", "admin_note"])
    for o in db().execute("SELECT * FROM orders ORDER BY id"):
        items = "; ".join(f"{i['quantity']}x {i['product']} ({i['variant']})" for i in json.loads(o["items_json"]))
        w.writerow([_csv_safe(x) for x in [
            1000 + o["id"], o["created_at"], o["status"], o["payment_status"], o["payment_method"], o["name"],
            o["email"], o["phone"], o["address"], o["city"], o["state"], o["pincode"], items, o["subtotal"],
            o["discount_code"], o["discount_amount"], o["shipping"], o["cod_fee"], o["total"], o["tracking"],
            o["notes"], o["admin_note"]]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})


@bp.route("/backup.db")
def backup():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    dst = sqlite3.connect(tmp.name)
    db().backup(dst)
    dst.close()
    data = Path(tmp.name).read_bytes()
    os.unlink(tmp.name)
    return Response(data, mimetype="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=store-{datetime.now():%Y%m%d-%H%M}.db"})


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
@bp.route("/products")
def products():
    doc = core.read_doc("products.json")
    core.inventory()
    rows = []
    for p in doc["products"]:
        left = [core.stock_of(v) for v in p["variants"]]
        counted = [x for x in left if x is not None]
        rows.append({"p": p, "price": min(v["price"] for v in p["variants"]),
                     "stock": sum(counted) if counted else None,
                     "cols": [c["title"] for c in doc["collections"] if c["handle"] in p.get("collections", [])]})
    return render("admin_products.html", "products", rows=rows)


def blank_product():
    return {"title": "", "handle": "", "vendor": core.load_json("site.json").get("brand_text", ""),
            "description": "", "collections": [], "images": [], "variants": [], "hidden": False}


def product_rows(p):
    """Variant rows for the form, using live stock."""
    rows = []
    for v in p["variants"]:
        live = core.stock_of(v) if "stock" in v else None
        rows.append({"id": v["id"], "title": "" if v["title"] == "Default Title" else v["title"],
                     "price": v["price"], "compare": v.get("compare_at_price") or "",
                     "stock": "" if live is None else live, "orig": "" if live is None else live,
                     "avail": "0" if v.get("available") is False else "1"})
    return rows


def parse_product_form(doc, existing):
    f, errors = request.form, []
    title = f.get("title", "").strip()[:120]
    if not title:
        errors.append("Please enter a product title.")
    if existing:
        handle = existing["handle"]
    else:
        handle = slugify(f.get("handle", "") or title)
        if not handle:
            errors.append("Use some letters or numbers in the title so we can make a page address.")
        elif any(p["handle"] == handle for p in doc["products"]):
            errors.append(f"A product with the address “{handle}” already exists. Change the URL name.")
    valid_cols = {c["handle"] for c in doc["collections"]}
    cols = [c for c in f.getlist("collections") if c in valid_cols and c != "all"]

    def col(name):
        return f.getlist(name)

    def at(lst, i):
        return lst[i].strip() if i < len(lst) else ""

    n = len(col("v_title"))
    rows, used = [], []
    for i in range(n):
        row = {"id": at(col("v_id"), i), "title": at(col("v_title"), i)[:60], "price": at(col("v_price"), i),
               "compare": at(col("v_compare"), i), "stock": at(col("v_stock"), i),
               "orig": at(col("v_orig"), i), "avail": at(col("v_avail"), i) or "1"}
        rows.append(row)
        if row["title"] or row["price"] or row["compare"] or row["stock"]:
            used.append(row)
    if not used:
        errors.append("Add at least one price (and a size/option name if the product has several).")
    single = len(used) == 1 and not used[0]["title"]
    owned = {v["id"] for v in existing["variants"]} if existing else set()
    all_ids = {v["id"] for p in doc["products"] for v in p["variants"]}
    variants, seen = [], set()
    for row in used:
        name = "Default Title" if single else row["title"]
        label = name if name != "Default Title" else "this product"
        if not name:
            errors.append("Every row needs an option name (e.g. S, M, L) when a product has more than one.")
            continue
        if name.lower() in seen:
            errors.append(f"The option “{name}” appears twice.")
            continue
        seen.add(name.lower())
        price = parse_money(row["price"])
        if price is None:
            errors.append(f"Enter a whole-rupee price for “{label}”.")
            continue
        compare = None
        if row["compare"]:
            compare = parse_money(row["compare"])
            if compare is None or compare <= price:
                errors.append(f"The “was” price for “{label}” must be a number higher than its price.")
                continue
        stock = None
        if row["stock"] != "":
            stock = parse_int(row["stock"], 0, 999999)
            if stock is None:
                errors.append(f"Stock for “{label}” must be a whole number (or leave blank for unlimited).")
                continue
        vid = row["id"] if row["id"] in owned else ""
        while not vid or (vid in all_ids and vid not in owned):
            vid = f"{handle or 'item'}-{secrets.token_hex(3)}"
        all_ids.add(vid)
        v = {"id": vid, "title": name, "price": price, "compare_at_price": compare}
        if stock is not None:
            v["stock"] = stock
        if row["avail"] == "0":
            v["available"] = False
        variants.append(v)
    product = {"handle": handle, "title": title,
               "vendor": f.get("vendor", "").strip()[:80] or core.load_json("site.json").get("brand_text", ""),
               "description": to_html(f.get("description", "")), "collections": cols,
               "images": list(existing["images"]) if existing else [], "variants": variants}
    if f.get("hidden") == "1":
        product["hidden"] = True
    return product, rows, errors


def sync_inventory(old, new, rows):
    """Apply stock numbers typed into the product form to the live stock table."""
    conn = db()
    orig = {r["id"]: r["orig"] for r in rows if r["id"]}
    old_ids = {v["id"] for v in old["variants"]} if old else set()
    for gone in old_ids - {v["id"] for v in new["variants"]}:
        conn.execute("DELETE FROM inventory WHERE variant_id=?", (gone,))
    for v in new["variants"]:
        has = conn.execute("SELECT stock FROM inventory WHERE variant_id=?", (v["id"],)).fetchone()
        if "stock" in v:
            if not has:
                conn.execute("INSERT INTO inventory VALUES (?,?)", (v["id"], v["stock"]))
            elif str(orig.get(v["id"], "")) != str(v["stock"]):     # only if the owner actually changed it
                conn.execute("UPDATE inventory SET stock=? WHERE variant_id=?", (v["stock"], v["id"]))
        elif has:
            conn.execute("DELETE FROM inventory WHERE variant_id=?", (v["id"],))
    conn.commit()
    g.pop("inv", None)


@bp.route("/products/new", methods=["GET", "POST"], defaults={"handle": None})
@bp.route("/products/<handle>/edit", methods=["GET", "POST"])
def product_form(handle):
    doc = core.read_doc("products.json")
    existing = next((p for p in doc["products"] if p["handle"] == handle), None) if handle else None
    if handle and not existing:
        abort(404)
    collections = [c for c in doc["collections"] if c["handle"] != "all"]

    if request.method == "POST":
        product, rows, errors = parse_product_form(doc, existing)
        if errors:
            for e in errors:
                flash(e, "error")
            if existing:
                product["images"] = existing["images"]
            return render("admin_product.html", "products", p=product, rows=rows + [{}] * 2, is_new=not existing,
                          collections=collections), 400
        # images: remove ticked, add new uploads, pick main
        removed = set(request.form.getlist("remove_image"))
        images = [i for i in product["images"] if i not in removed]
        for fs in request.files.getlist("new_images"):
            if fs and fs.filename:
                try:
                    images.append(save_image(fs))
                except ValueError as e:
                    flash(f"Skipped image — {e}", "error")
        main = request.form.get("main_image")
        if main in images:
            images.remove(main)
            images.insert(0, main)
        product["images"] = images
        if existing:
            doc["products"][doc["products"].index(existing)] = product
        else:
            doc["products"].insert(0, product)                 # newest first
        core.write_doc("products.json", doc)
        sync_inventory(existing, product, rows)
        for path in removed:
            delete_upload_if_unused(path)
        flash(f"Saved “{product['title']}”." + (" It is hidden from the store." if product.get("hidden") else ""))
        return redirect(url_for("adm.products"))

    product = existing or blank_product()
    rows = product_rows(product) if existing else []
    return render("admin_product.html", "products", p=product, rows=rows + [{}] * (2 if existing else 4),
                  is_new=not existing, collections=collections)


@bp.post("/products/<handle>/delete")
def product_delete(handle):
    doc = core.read_doc("products.json")
    p = next((x for x in doc["products"] if x["handle"] == handle), None)
    if not p:
        abort(404)
    doc["products"].remove(p)
    core.write_doc("products.json", doc)
    for v in p["variants"]:
        db().execute("DELETE FROM inventory WHERE variant_id=?", (v["id"],))
    db().commit()
    g.pop("inv", None)
    for img in p.get("images", []):
        delete_upload_if_unused(img)
    flash(f"Deleted “{p['title']}”. Past orders keep their record.")
    return redirect(url_for("adm.products"))


@bp.post("/products/<handle>/duplicate")
def product_duplicate(handle):
    doc = core.read_doc("products.json")
    p = next((x for x in doc["products"] if x["handle"] == handle), None)
    if not p:
        abort(404)
    taken = {x["handle"] for x in doc["products"]}
    new_handle, n = f"{handle}-copy", 2
    while new_handle in taken:
        new_handle, n = f"{handle}-copy-{n}", n + 1
    clone = json.loads(json.dumps(p))
    clone.update(handle=new_handle, title=p["title"] + " (copy)", hidden=True)
    for v in clone["variants"]:
        v["id"] = f"{new_handle}-{secrets.token_hex(3)}"
    doc["products"].insert(doc["products"].index(p) + 1, clone)
    core.write_doc("products.json", doc)
    flash("Duplicated as a hidden draft — edit it, then untick “Hide from store”.")
    return redirect(url_for("adm.product_form", handle=new_handle))


# --------------------------------------------------------------------------
# Stock (quick editor)
# --------------------------------------------------------------------------
@bp.route("/stock")
def inventory():
    core.inventory()
    counted = [(p, v, core.stock_of(v)) for p in core.read_doc("products.json")["products"]
               for v in p["variants"] if "stock" in v]
    return render("admin_inventory.html", "inventory", counted=counted)


@bp.post("/stock")
def inventory_save():
    core.inventory()
    conn = db()
    n = 0
    for p in core.read_doc("products.json")["products"]:
        for v in p["variants"]:
            raw = request.form.get(f"stock_{v['id']}")
            if "stock" in v and raw is not None and raw.strip().isdigit():
                conn.execute("UPDATE inventory SET stock=? WHERE variant_id=?", (int(raw), v["id"]))
                n += 1
    conn.commit()
    g.pop("inv", None)
    flash("Stock updated.")
    return redirect(url_for("adm.inventory"))


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------
@bp.route("/collections", methods=["GET", "POST"])
def collections():
    doc = core.read_doc("products.json")
    if request.method == "POST":
        action, f = request.form.get("action"), request.form
        cols = doc["collections"]
        if action == "add":
            title = f.get("title", "").strip()[:60]
            handle = slugify(f.get("handle", "") or title)
            if not title or not handle:
                flash("Give the collection a title.", "error")
            elif handle == "all" or any(c["handle"] == handle for c in cols):
                flash(f"A collection with the address “{handle}” already exists.", "error")
            else:
                cols.append({"handle": handle, "title": title, "description": f.get("description", "").strip()[:300]})
                core.write_doc("products.json", doc)
                flash(f"Added collection “{title}”.")
        elif action == "save":
            c = next((x for x in cols if x["handle"] == f.get("handle")), None)
            title = f.get("title", "").strip()[:60]
            if c and title:
                c["title"], c["description"] = title, f.get("description", "").strip()[:300]
                core.write_doc("products.json", doc)
                flash("Collection saved.")
        elif action == "delete":
            h = f.get("handle")
            if h != "all" and any(c["handle"] == h for c in cols):
                doc["collections"] = [c for c in cols if c["handle"] != h]
                for p in doc["products"]:
                    p["collections"] = [x for x in p.get("collections", []) if x != h]
                core.write_doc("products.json", doc)
                flash("Collection deleted. Products stay in your store; update any menu links to it in Settings.")
        return redirect(url_for("adm.collections"))
    counts = {c["handle"]: sum(1 for p in doc["products"] if c["handle"] in p.get("collections", []))
              for c in doc["collections"]}
    counts["all"] = len(doc["products"])
    return render("admin_collections.html", "collections", collections=doc["collections"], counts=counts)


# --------------------------------------------------------------------------
# Discount codes
# --------------------------------------------------------------------------
def parse_discount(f):
    errors = []
    code = f.get("code", "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,30}", code):
        errors.append("Code must be 3–30 letters/numbers (e.g. DIWALI10).")
    typ = f.get("type")
    if typ not in ("percent", "flat", "free_shipping"):
        errors.append("Choose a discount type.")
    d = {"type": typ, "active": f.get("active") == "1", "description": f.get("description", "").strip()[:120]}
    if typ in ("percent", "flat"):
        value = parse_int(f.get("value"), 1, 100 if typ == "percent" else 1000000)
        if value is None:
            errors.append("Enter the amount: 1–100 for a percentage, or a rupee amount.")
        else:
            d["value"] = value
    if f.get("min_order", "").strip():
        m = parse_int(f.get("min_order"), 0, 10000000)
        if m is None:
            errors.append("Minimum order must be a whole number of rupees.")
        else:
            d["min_order"] = m
    if f.get("expires", "").strip():
        try:
            date.fromisoformat(f["expires"].strip())
            d["expires"] = f["expires"].strip()
        except ValueError:
            errors.append("Expiry must be a valid date.")
    if f.get("max_uses", "").strip():
        m = parse_int(f.get("max_uses"), 1, 10000000)
        if m is None:
            errors.append("Maximum uses must be a whole number.")
        else:
            d["max_uses"] = m
    if f.get("once_per_customer") == "1":
        d["once_per_customer"] = True
    return code, d, errors


@bp.route("/discounts", methods=["GET", "POST"])
def discounts():
    docs = core.read_doc("discounts.json", {})
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            docs.pop(request.form.get("code", ""), None)
            core.write_doc("discounts.json", docs)
            flash("Discount code deleted.")
        elif action in ("add", "save"):
            code, d, errors = parse_discount(request.form)
            if action == "add" and code in docs:
                errors.append(f"The code {code} already exists — edit it below instead.")
            if action == "save" and code not in docs:
                errors.append("That code no longer exists.")
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                docs[code] = d
                core.write_doc("discounts.json", docs)
                flash(f"{'Added' if action == 'add' else 'Saved'} code {code}.")
        return redirect(url_for("adm.discounts"))
    used = {r["discount_code"]: r["c"] for r in db().execute(
        "SELECT discount_code, COUNT(*) c FROM orders WHERE discount_code IS NOT NULL AND status!='cancelled' "
        "GROUP BY discount_code")}
    return render("admin_discounts.html", "discounts", codes=docs, used=used, today=date.today().isoformat())


# --------------------------------------------------------------------------
# Pages (About, Shipping, Returns, Privacy, Terms, anything else)
# --------------------------------------------------------------------------
@bp.route("/pages")
def pages():
    return render("admin_pages.html", "pages", pages=core.read_doc("pages.json", {}))


@bp.route("/pages/new", methods=["GET", "POST"], defaults={"key": None})
@bp.route("/pages/<key>/edit", methods=["GET", "POST"])
def page_form(key):
    docs = core.read_doc("pages.json", {})
    existing = docs.get(key) if key else None
    if key and existing is None:
        abort(404)
    if request.method == "POST":
        f, errors = request.form, []
        title = f.get("title", "").strip()[:100]
        if not title:
            errors.append("Please give the page a title.")
        slug = key or slugify(f.get("key", "") or title)
        if not slug:
            errors.append("Use some letters or numbers in the title.")
        elif not key and slug in docs:
            errors.append(f"A page with the address “{slug}” already exists.")
        page = {"title": title, "content": to_html(f.get("content", ""))}
        if f.get("draft") == "1":
            page["draft"] = True
        if errors:
            for e in errors:
                flash(e, "error")
            return render("admin_page.html", "pages", key=key, page=dict(page, key=slug), is_new=not key), 400
        docs[slug] = page
        core.write_doc("pages.json", docs)
        flash(f"Saved page “{title}”. It lives at /pages/{slug}.")
        return redirect(url_for("adm.pages"))
    return render("admin_page.html", "pages", key=key, page=existing or {"title": "", "content": ""}, is_new=not key)


@bp.post("/pages/<key>/delete")
def page_delete(key):
    docs = core.read_doc("pages.json", {})
    if key in docs:
        docs.pop(key)
        core.write_doc("pages.json", docs)
        flash("Page deleted. Remove any menu links to it in Settings.")
    return redirect(url_for("adm.pages"))


# --------------------------------------------------------------------------
# Customers (accounts)
# --------------------------------------------------------------------------
@bp.route("/customers", methods=["GET", "POST"])
def customers():
    conn = db()
    if request.method == "POST":
        cid, action = request.form.get("id", type=int), request.form.get("action")
        row = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone() if cid else None
        if not row:
            flash("Customer not found.", "error")
        elif action == "verify":
            conn.execute("UPDATE customers SET email_verified=1 WHERE id=?", (cid,))
            conn.commit()
            flash(f"Marked {row['email']} as verified.")
        elif action == "delete":
            core._accounts.delete_customer(conn, cid)
            flash(f"Deleted the account for {row['email']}. Their past orders are kept, unlinked.")
        return redirect(url_for("adm.customers", q=request.args.get("q") or None))
    q = request.args.get("q", "").strip()[:80]
    like = f"%{q}%"
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id=c.id) AS orders,
                  (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.customer_id=c.id
                     AND o.status NOT IN ('cancelled','pending_payment')) AS spent
           FROM customers c WHERE (?='' OR c.email LIKE ? OR c.name LIKE ?) ORDER BY c.id DESC LIMIT 300""",
        (q, like, like)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    return render("admin_customers.html", "customers", customers=rows, q=q, total=total)


# --------------------------------------------------------------------------
# Abandoned carts
# --------------------------------------------------------------------------
@bp.route("/carts")
def carts():
    names = {v["id"]: (p["title"] + ("" if v["title"] == "Default Title" else f" ({v['title']})"))
             for p in core.read_doc("products.json")["products"] for v in p["variants"]}
    rows = []
    for r in db().execute("SELECT * FROM carts ORDER BY updated_at DESC LIMIT 100").fetchall():
        items = json.loads(r["items_json"])
        rows.append(dict(r, summary=", ".join(f"{q} × {names.get(v, 'removed item')}" for v, q in items.items())))
    from carts import config
    return render("admin_carts.html", "carts", rows=rows, stats=core._carts.stats(), cfg=config(),
                  smtp=core.smtp_configured(), site_url=core.site_url(),
                  cron=bool(os.environ.get("CRON_SECRET")), scheduler=os.environ.get("ENABLE_SCHEDULER") == "1")


@bp.post("/carts/run")
def carts_run():
    res = core._carts.run_job()
    if "skipped" in res:
        flash("Nothing sent — " + res["skipped"], "error")
    else:
        flash(f"Reminder run finished: {res['sent']} sent, {res['recovered']} already ordered, "
              f"{res['retired']} old bags retired, {res['failed']} failed.")
    return redirect(url_for("adm.carts"))


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
@bp.route("/messages", methods=["GET", "POST"])
def messages():
    if request.method == "POST":
        mid = request.form.get("id", type=int)
        if mid:
            db().execute("DELETE FROM messages WHERE id=?", (mid,))
            db().commit()
            flash("Message deleted.")
        return redirect(url_for("adm.messages"))
    rows = db().execute("SELECT * FROM messages ORDER BY id DESC LIMIT 300").fetchall()
    return render("admin_messages.html", "messages", messages=rows)


# --------------------------------------------------------------------------
# Site settings
# --------------------------------------------------------------------------
def site_to_form(site):
    hero, feat, ship, biz, an, ct = (site["hero"], site["featured"], site["shipping"], site["business"],
                                     site["analytics"], site["contact"])
    cod = next((m for m in site["payment_methods"] if m["id"] == "cod"), {})
    ac = dict({"enabled": True, "first_after_hours": 2, "send_second": True, "second_after_hours": 24,
               "discount_code": ""}, **site.get("abandoned_cart", {}))
    return {"shop_name": site["shop_name"], "brand_text": site.get("brand_text", ""),
            "announcement_text": site.get("announcement_text", ""), "contact_email": site["contact_email"],
            "footer_tagline": site.get("footer_tagline", ""), "background_color": site["background_color"],
            "meta_description": site.get("meta_description", ""),
            "nav": menu_to_lines(site["nav"]), "footer_nav": menu_to_lines(site["footer_nav"]),
            "hero_eyebrow": hero.get("eyebrow", ""), "hero_heading": hero.get("heading", ""),
            "hero_body": hero.get("body", ""), "hero_button_label": hero.get("button_label", ""),
            "hero_button_link": hero.get("button_link", ""),
            "featured_eyebrow": feat.get("eyebrow", ""), "featured_heading": feat.get("heading", ""),
            "featured_collection": feat.get("collection", "all"), "featured_limit": feat.get("limit", 4),
            "ship_flat_rate": ship["flat_rate"], "ship_free_over": ship["free_over"],
            "ship_cod_fee": ship.get("cod_fee", 0), "cod_instructions": cod.get("instructions", ""),
            "contact_heading": ct["heading"], "contact_intro": ct["intro"], "contact_topics": "\n".join(ct["topics"]),
            "biz_legal_name": biz.get("legal_name", ""), "biz_address": biz.get("address", ""),
            "biz_phone": biz.get("phone", ""), "biz_gstin": biz.get("gstin", ""),
            "ga4_id": an.get("ga4_id", ""), "plausible_domain": an.get("plausible_domain", ""),
            **{"ac_enabled": "1" if ac["enabled"] else "", "ac_first": ac["first_after_hours"],
               "ac_second_on": "1" if ac["send_second"] else "", "ac_second": ac["second_after_hours"],
               "ac_code": ac["discount_code"]}}


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    site = core.read_doc("site.json")
    cols = core.read_doc("products.json")["collections"]
    if request.method == "POST":
        f, errors = request.form, []
        g_ = lambda k, n=200: f.get(k, "").strip()[:n]            # noqa: E731
        if not g_("shop_name"):
            errors.append("Shop name can’t be empty.")
        if not core.EMAIL_RE.match(g_("contact_email")):
            errors.append("Enter a valid contact email.")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", g_("background_color")):
            errors.append("Background colour must look like #f7f5f0.")
        nav = lines_to_menu(f.get("nav"), "Header menu", errors)
        footer_nav = lines_to_menu(f.get("footer_nav"), "Footer menu", errors)
        flat, free, cod_fee = (parse_int(f.get("ship_flat_rate"), 0, 100000), parse_int(f.get("ship_free_over"), 0, 10000000),
                               parse_int(f.get("ship_cod_fee") or "0", 0, 10000))
        if None in (flat, free, cod_fee):
            errors.append("Shipping amounts must be whole rupee numbers.")
        limit = parse_int(f.get("featured_limit"), 1, 12)
        if limit is None:
            errors.append("“Products to show on the home page” must be 1–12.")
        if g_("featured_collection") not in {c["handle"] for c in cols}:
            errors.append("Choose a featured collection.")
        topics = [t.strip()[:60] for t in f.get("contact_topics", "").splitlines() if t.strip()]
        if not topics:
            errors.append("Add at least one contact-form topic.")
        ga4 = g_("ga4_id")
        if ga4 and not re.fullmatch(r"G-[A-Z0-9]{4,15}", ga4):
            errors.append("Google Analytics ID looks like G-XXXXXXXXXX (or leave blank).")
        pl = g_("plausible_domain")
        if pl and not re.fullmatch(r"[a-z0-9.-]{3,100}", pl):
            errors.append("Plausible domain should look like yourshop.in (or leave blank).")
        discounts_doc = core.read_doc("discounts.json", {})
        ac_present = "ac_first" in f                  # an older cached settings page won't send these: keep current
        ac_first, ac_second = parse_int(f.get("ac_first"), 1, 72), parse_int(f.get("ac_second"), 2, 168)
        ac_second_on = f.get("ac_second_on") == "1"
        if ac_present and (ac_first is None or ac_second is None):
            errors.append("Cart-reminder delays must be whole hours (first 1–72, second 2–168).")
        elif ac_present and ac_second_on and ac_second <= ac_first:
            errors.append("The second cart reminder must be later than the first.")
        if ac_present and g_("ac_code") and g_("ac_code") not in discounts_doc:
            errors.append("Choose a discount code from the list for the second reminder (or none).")
        for k in ("hero_button_link",):
            if g_(k) and not re.match(r"^(/|https?://)", g_(k)):
                errors.append("The hero button link must start with / or https://")
        if errors:
            for e in errors:
                flash(e, "error")
            return render("admin_settings.html", "settings", form=f.to_dict(), collections=cols,
                          codes=list(discounts_doc), hero_image=site["hero"].get("image")), 400

        site.update(shop_name=g_("shop_name", 80), brand_text=g_("brand_text", 60), announcement_text=g_("announcement_text"),
                    contact_email=g_("contact_email"), footer_tagline=g_("footer_tagline"),
                    background_color=g_("background_color"), meta_description=g_("meta_description", 300),
                    nav=nav, footer_nav=footer_nav)
        hero = site["hero"]
        hero.update(eyebrow=g_("hero_eyebrow", 60), heading=g_("hero_heading", 120), body=g_("hero_body", 300),
                    button_label=g_("hero_button_label", 40), button_link=g_("hero_button_link", 300))
        old_hero = hero.get("image")
        up = request.files.get("hero_image")
        if up and up.filename:
            try:
                hero["image"] = save_image(up)
            except ValueError as e:
                flash(f"Hero image skipped — {e}", "error")
        elif f.get("remove_hero_image") == "1":
            hero["image"] = ""
        site["featured"].update(eyebrow=g_("featured_eyebrow", 60), heading=g_("featured_heading", 80),
                                collection=g_("featured_collection"), limit=limit)
        site["shipping"].update(flat_rate=flat, free_over=free, cod_fee=cod_fee)
        for m in site["payment_methods"]:
            if m["id"] == "cod":
                m["instructions"] = g_("cod_instructions", 200)
        site["contact"].update(heading=g_("contact_heading", 120), intro=g_("contact_intro", 400), topics=topics)
        site["business"] = {"legal_name": g_("biz_legal_name", 120), "address": g_("biz_address", 250),
                            "phone": g_("biz_phone", 30), "gstin": g_("biz_gstin", 20)}
        site["analytics"] = {"ga4_id": ga4, "plausible_domain": pl}
        if ac_present:
            site["abandoned_cart"] = {"enabled": f.get("ac_enabled") == "1", "first_after_hours": ac_first,
                                      "send_second": ac_second_on, "second_after_hours": ac_second,
                                      "discount_code": g_("ac_code")}
        core.write_doc("site.json", site)
        if hero.get("image") != old_hero:
            delete_upload_if_unused(old_hero)
        flash("Settings saved.")
        return redirect(url_for("adm.settings"))
    return render("admin_settings.html", "settings", form=site_to_form(site), collections=cols,
                  codes=list(core.read_doc("discounts.json", {})), hero_image=site["hero"].get("image"))
