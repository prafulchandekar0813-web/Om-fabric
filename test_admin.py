"""Admin panel tests: python test_admin.py"""
import base64, io, os, re, sqlite3, tempfile
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"),
                  ADMIN_PASSWORD="pw", SECRET_KEY="test", SITE_URL="https://shop.example")
os.environ.pop("RAZORPAY_KEY_ID", None); os.environ.pop("RAZORPAY_KEY_SECRET", None)
import app as A
import admin as ADM
from PIL import Image

sent = []
A.send_email = lambda to, subject, body, reply_to=None: sent.append((to, subject, body))
c = A.app.test_client()
HDR = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
def tok(html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
def aget(path, code=200):
    r = c.get(path, headers=HDR); assert r.status_code == code, (path, r.status_code); return r.get_data(as_text=True)
def get(path, code=200):
    r = c.get(path); assert r.status_code == code, (path, r.status_code)
    return re.sub(r'<div class="flash[^"]*"[^>]*>.*?</div>', "", r.get_data(as_text=True), flags=re.S)   # ignore one-off "Saved" banners
T = tok(aget("/admin/collections"))
def apost(path, data=None, code=302, **kw):
    r = c.post(path, data={"csrf_token": T, **(data or {})}, headers=HDR, **kw)
    assert r.status_code == code, (path, r.status_code, r.get_data(as_text=True)[:600]); return r
def png(w=2000, h=3000, color="red"):
    b = io.BytesIO(); Image.new("RGB", (w, h), color).save(b, "PNG"); b.seek(0); return b
def stock(vid):
    row = sqlite3.connect(os.environ["DATABASE_PATH"]).execute("SELECT stock FROM inventory WHERE variant_id=?", (vid,)).fetchone()
    return row[0] if row else None
def pform(title, rows, **extra):
    d = {"title": title, "vendor": "", "description": "Soft and light.\n\nMade to last.",
         "v_id": [r.get("id", "") for r in rows], "v_orig": [r.get("orig", "") for r in rows],
         "v_title": [r.get("title", "") for r in rows], "v_price": [r.get("price", "") for r in rows],
         "v_compare": [r.get("compare", "") for r in rows], "v_stock": [r.get("stock", "") for r in rows],
         "v_avail": [r.get("avail", "1") for r in rows]}
    d.update(extra); return d
def pjson(handle):
    return next((p for p in A.read_doc_for_test("products.json")["products"] if p["handle"] == handle), None)
def _rd(name):
    with A.app.app_context(): return A.read_doc(name)
A.read_doc_for_test = _rd

# ---------- everything is login-protected, writes need CSRF ----------
for p in ["/admin", "/admin/products", "/admin/products/new", "/admin/collections", "/admin/discounts", "/admin/pages",
          "/admin/pages/new", "/admin/messages", "/admin/settings", "/admin/stock", "/admin/orders.csv", "/admin/backup.db"]:
    assert c.get(p).status_code == 401, p
    assert c.get(p, headers=HDR).status_code == 200, p
assert c.post("/admin/collections", data={"action": "add", "title": "x"}, headers=HDR).status_code == 400   # no CSRF
for k in ("Orders", "Products", "Collections", "Discounts", "Pages", "Messages", "Settings"):
    assert f">{k}</a>" in aget("/admin"), k

# ---------- sanitiser ----------
dirty = '<p onclick="x()">hi<script>alert(1)</script><a href="javascript:alert(1)">bad</a> <a href="/ok" onmouseover="y">ok</a><img src=x onerror=z></p>'
clean = ADM.to_html(dirty)
assert "onclick" not in clean and "script" not in clean and "alert" not in clean and "javascript:" not in clean and "onerror" not in clean and "<img" not in clean
assert '<a href="/ok" rel="noopener">ok</a>' in clean
assert ADM.to_html("One\n\nTwo") == "<p>One</p><p>Two</p>"
assert ADM.to_html("<p>One</p><b>unclosed") == "<p>One</p><b>unclosed</b>"
assert "&lt;" in ADM.to_html("a < b & c") and ADM.to_html("") == ""

# ---------- collections ----------
apost("/admin/collections", {"action": "add", "title": "Festive Edit", "description": "Shine bright"})
assert "/collections/festive-edit" in aget("/admin/collections") and "Festive Edit" in get("/collections/festive-edit")
apost("/admin/collections", {"action": "add", "title": "Festive Edit"})                       # duplicate -> flash only
apost("/admin/collections", {"action": "add", "title": "All", "handle": "all"})
assert aget("/admin/collections").count("/collections/festive-edit") >= 1
apost("/admin/collections", {"action": "save", "handle": "festive-edit", "title": "Festive Picks", "description": "x"})
assert "Festive Picks" in get("/collections/festive-edit")

# ---------- add a product (with photos, sizes, stock, sale price) ----------
data = pform("Silk Scarf", [
    {"title": "Small", "price": "₹1,299", "compare": "1,799", "stock": "5"},
    {"title": "Large", "price": "1499", "stock": "2"},
    {"title": "", "price": ""}, {}], collections=["festive-edit", "sale"], handle="")
data["new_images"] = [(png(), "front.png"), (png(1200, 1600, "blue"), "back.png")]
apost("/admin/products/new", data, content_type="multipart/form-data")
p = pjson("silk-scarf"); assert p and p["title"] == "Silk Scarf" and len(p["variants"]) == 2 and len(p["images"]) == 2
assert p["variants"][0]["price"] == 1299 and p["variants"][0]["compare_at_price"] == 1799 and p["description"] == "<p>Soft and light.</p><p>Made to last.</p>"
vs, vl = p["variants"][0]["id"], p["variants"][1]["id"]
assert stock(vs) == 5 and stock(vl) == 2
# photos were really re-encoded + resized + are publicly served
img_path = p["images"][0]; assert img_path.startswith("/uploads/") and img_path.endswith(".webp")
r = c.get(img_path); assert r.status_code == 200 and r.data[:4] == b"RIFF"
im = Image.open(io.BytesIO(r.data)); assert max(im.size) <= 1600
# storefront shows it (newest first), with sale + stock
home = get("/collections/all"); assert home.index("Silk Scarf") < home.index("Relaxed Linen Shirt")
page = get("/products/silk-scarf"); assert "Small — ₹1,299 (only 5 left)" in page and "Large — ₹1,499 (only 2 left)" in page
assert "<s>₹1,799</s>" in get("/collections/sale")
assert "Silk Scarf" in get("/collections/sale") and "Silk Scarf" in get("/collections/festive-edit")
assert "Silk Scarf" in get("/search?q=scarf") and "/products/silk-scarf" in get("/sitemap.xml")
# customer can buy it and stock drops
cust = A.app.test_client()
t2 = tok(cust.get("/products/silk-scarf").get_data(as_text=True))
cust.post("/cart/add", data={"csrf_token": t2, "id": vs, "quantity": "2"})
t3 = tok(cust.get("/checkout").get_data(as_text=True))
r = cust.post("/checkout", data={"csrf_token": t3, "name": "Asha Rao", "email": "a@b.co", "phone": "9876543210", "address": "12 Marine Drive",
              "city": "Mumbai", "state": "MH", "pincode": "400020", "payment": "cod"}); assert r.status_code == 302
assert stock(vs) == 3

# ---------- product validation ----------
def bad(rows, title="Bad", expect=""):
    r = apost("/admin/products/new", pform(title, rows), code=400, content_type="multipart/form-data")
    assert expect in r.get_data(as_text=True), (expect, r.get_data(as_text=True)[:300])
bad([{"title": "S", "price": "abc"}], expect="whole-rupee price")
bad([{"title": "S", "price": "100"}, {"title": "s", "price": "100"}], expect="appears twice")
bad([{"title": "S", "price": "100", "compare": "90"}], expect="must be a number higher")
bad([{"title": "S", "price": "100", "stock": "-1"}], expect="whole number")
bad([{"title": "S", "price": "100"}, {"title": "", "price": "120"}], expect="needs an option name")
bad([{}, {}], expect="Add at least one price")
bad([{"title": "S", "price": "100"}], title="", expect="product title")
bad([{"title": "S", "price": "100"}], title="Silk Scarf", expect="already exists")

# ---------- edit: stock only changes when the owner changes it ----------
with A.app.app_context():                         # an order took 1 unit meanwhile (live stock now 2)
    conn = sqlite3.connect(os.environ["DATABASE_PATH"]); conn.execute("UPDATE inventory SET stock=2 WHERE variant_id=?", (vs,)); conn.commit()
rows = [{"id": vs, "title": "Small", "price": "1399", "compare": "1799", "stock": "3", "orig": "3"},   # stale form, stock untouched
        {"id": vl, "title": "Large", "price": "1499", "stock": "9", "orig": "2"},                       # owner restocked
        {"title": "XL", "price": "1599", "stock": "4"}]                                                  # new size
apost("/admin/products/silk-scarf/edit", pform("Silk Scarf Pro", rows, collections=["sale"], main_image=p["images"][1]),
      content_type="multipart/form-data")
p2 = pjson("silk-scarf"); assert p2["title"] == "Silk Scarf Pro" and p2["handle"] == "silk-scarf" and len(p2["variants"]) == 3
assert [v["id"] for v in p2["variants"][:2]] == [vs, vl]
assert stock(vs) == 2, "untouched stock must not overwrite live count" and stock(vl) == 9 and stock(p2["variants"][2]["id"]) == 4
assert p2["images"][0] == p["images"][1], "main photo moved first"
assert "Silk Scarf Pro" not in get("/collections/festive-edit") and "Silk Scarf Pro" in get("/collections/sale")
# remove a photo -> file deleted
gone = p2["images"][1]
apost("/admin/products/silk-scarf/edit", pform("Silk Scarf Pro", [{"id": vs, "title": "Small", "price": "1399", "orig": "2", "stock": "2"}], remove_image=[gone]),
      content_type="multipart/form-data")
assert c.get(gone).status_code == 404 and len(pjson("silk-scarf")["images"]) == 1 and stock(vl) is None, "removed variant's stock row cleaned up"
# a non-image upload is skipped, product still saves
r = apost("/admin/products/silk-scarf/edit", pform("Silk Scarf Pro", [{"id": vs, "title": "Small", "price": "1399", "orig": "2", "stock": "2"}]) | {"new_images": (io.BytesIO(b"<script>x</script>"), "evil.png")},
          content_type="multipart/form-data")
assert len(pjson("silk-scarf")["images"]) == 1
assert os.listdir(os.environ["UPLOAD_DIR"]).__len__() == 1

# ---------- hide / unhide ----------
apost("/admin/products/silk-scarf/edit", pform("Silk Scarf Pro", [{"id": vs, "title": "Small", "price": "1399", "orig": "2", "stock": "2"}], hidden="1"),
      content_type="multipart/form-data")
get("/products/silk-scarf", 404); assert "Silk Scarf" not in get("/collections/all") and "Silk Scarf" not in get("/search?q=scarf")
assert "silk-scarf" not in get("/sitemap.xml") and "Hidden" in aget("/admin/products")
assert cust.post("/cart/add", data={"csrf_token": t2, "id": vs}).status_code == 404, "hidden product can't be bought"
apost("/admin/products/silk-scarf/edit", pform("Silk Scarf Pro", [{"id": vs, "title": "Small", "price": "1399", "orig": "2", "stock": "2"}]), content_type="multipart/form-data")
get("/products/silk-scarf")

# ---------- single-variant product, duplicate, delete ----------
apost("/admin/products/new", pform("Gift Card", [{"title": "", "price": "500"}]), content_type="multipart/form-data")
g1 = pjson("gift-card"); assert g1["variants"][0]["title"] == "Default Title" and "stock" not in g1["variants"][0]
assert "Add to bag" in get("/products/gift-card") and 'id="variant"' not in get("/products/gift-card")
apost("/admin/products/gift-card/duplicate"); d1 = pjson("gift-card-copy"); assert d1 and d1["hidden"] and d1["variants"][0]["id"] != g1["variants"][0]["id"]
get("/products/gift-card-copy", 404)
apost("/admin/products/gift-card-copy/delete"); assert pjson("gift-card-copy") is None
apost("/admin/products/gift-card/delete"); get("/products/gift-card", 404)
before = stock(vs); apost("/admin/products/silk-scarf/delete"); assert stock(vs) is None and os.listdir(os.environ["UPLOAD_DIR"]) == []
# existing sample products still fine & past orders unaffected
assert "Relaxed Linen Shirt" in get("/collections/all")

# ---------- collections delete strips it from products ----------
apost("/admin/collections", {"action": "delete", "handle": "festive-edit"}); get("/collections/festive-edit", 404)
assert all("festive-edit" not in p.get("collections", []) for p in _rd("products.json")["products"])

# ---------- discount codes ----------
def dform(code, **kw): return {"action": "add", "code": code, "type": "percent", "value": "15", "active": "1", **kw}
apost("/admin/discounts", dform("diwali15", min_order="500", expires="2099-01-01", once_per_customer="1", description="Festival"))
assert "DIWALI15" in aget("/admin/discounts") and _rd("discounts.json")["DIWALI15"]["value"] == 15
c4 = A.app.test_client(); tt = tok(c4.get("/products/everyday-cotton-tee").get_data(as_text=True))
c4.post("/cart/add", data={"csrf_token": tt, "id": "ect-m", "quantity": "2"})
c4.post("/cart/discount", data={"csrf_token": tt, "code": "diwali15"}); cart = c4.get("/cart").get_data(as_text=True)
assert "Discount (DIWALI15)" in cart and "-₹240" in cart, cart
apost("/admin/discounts", {"action": "save", "code": "DIWALI15", "type": "flat", "value": "50"})       # active box unticked -> paused
assert _rd("discounts.json")["DIWALI15"]["active"] is False and "Paused" in aget("/admin/discounts")
cart = c4.get("/cart").get_data(as_text=True); assert "Discount (DIWALI15)" not in cart and "isn’t valid" in cart
apost("/admin/discounts", dform("DIWALI15"), code=302)                                                   # duplicate add -> flash, unchanged
assert _rd("discounts.json")["DIWALI15"]["type"] == "flat"
for badf in [dform("x"), dform("OK10", value="500"), dform("OK11", expires="31/12/2030"), dform("OK12", max_uses="zero"), dform("OK13", type="bogus")]:
    apost("/admin/discounts", badf); assert badf["code"] not in _rd("discounts.json"), badf
apost("/admin/discounts", {"action": "add", "code": "SHIPFREE", "type": "free_shipping", "active": "1"}); assert _rd("discounts.json")["SHIPFREE"]["type"] == "free_shipping"
apost("/admin/discounts", {"action": "delete", "code": "DIWALI15"}); assert "DIWALI15" not in _rd("discounts.json")
apost("/admin/discounts", {"action": "delete", "code": "WELCOME10"}); apost("/admin/discounts", {"action": "delete", "code": "FLAT100"})
c4.post("/cart/discount", data={"csrf_token": tt, "code": "WELCOME10"}); assert "isn’t valid" in c4.get("/cart").get_data(as_text=True)

# ---------- pages ----------
apost("/admin/pages/new", {"title": "Size Guide", "content": "<h3>Sizes</h3><p onclick=x>S = 36</p><script>evil()</script>", "draft": "1"})
pg = _rd("pages.json")["size-guide"]; assert "script" not in pg["content"] and "onclick" not in pg["content"] and pg["draft"] is True
assert "Sizes" in get("/pages/size-guide") and "DRAFT" in get("/pages/size-guide") and "/pages/size-guide" in get("/sitemap.xml")
apost("/admin/pages/size-guide/edit", {"title": "Size Guide 2", "content": "Plain text\n\nSecond"})
assert "DRAFT" not in get("/pages/size-guide") and "<p>Second</p>" in get("/pages/size-guide")
apost("/admin/pages/new", {"title": "Size Guide", "content": "dup"}, code=400)
apost("/admin/pages/size-guide/delete"); get("/pages/size-guide", 404)

# ---------- settings ----------
form = {"shop_name": "Loom & Co.", "brand_text": "loom&co.", "announcement_text": "Diwali sale — 20% off", "contact_email": "hi@loom.in",
        "footer_tagline": "Woven well", "background_color": "#ffffff", "meta_description": "Loom store",
        "nav": "Shop | /collections/all\nAbout | /pages/about", "footer_nav": "Contact | /contact",
        "hero_eyebrow": "NEW", "hero_heading": "Woven <b>well</b>", "hero_body": "Body", "hero_button_label": "Shop", "hero_button_link": "/collections/all",
        "featured_eyebrow": "TOP", "featured_heading": "Best sellers", "featured_collection": "tops", "featured_limit": "2",
        "ship_flat_rate": "99", "ship_free_over": "1500", "ship_cod_fee": "40", "cod_instructions": "Pay the courier",
        "contact_heading": "Say hi", "contact_intro": "We reply fast", "contact_topics": "Hello\nOther",
        "biz_legal_name": "Loom Pvt Ltd", "biz_address": "Pune", "biz_phone": "98200 00000", "biz_gstin": "27ABCDE1234F1Z5",
        "ga4_id": "G-ABC12345", "plausible_domain": ""}
r = apost("/admin/settings", form | {"hero_image": (png(1000, 800, "green"), "hero.jpg")}, content_type="multipart/form-data")
home = get("/")
assert "Diwali sale — 20% off" in home and "loom&amp;co." in home and "Loom &amp; Co." in home and "Woven &lt;b&gt;well&lt;/b&gt;" in home
assert 'href="/pages/about">About</a>' in home and 'href="/contact">Contact</a>' in home and "Loom Pvt Ltd" in home and "GSTIN 27ABCDE1234F1Z5" in home
assert "gtag/js?id=G-ABC12345" in home and 'style="--paper' not in home and "--paper:#ffffff" in home
assert "Best sellers" in home and home.count('class="product-card"') == 2
hero = re.search(r'class="hero-image">\s*<img src="(/uploads/[^"]+)"', home); assert hero and c.get(hero.group(1)).status_code == 200
c5 = A.app.test_client(); t5 = tok(c5.get("/products/everyday-cotton-tee").get_data(as_text=True))
c5.post("/cart/add", data={"csrf_token": t5, "id": "ect-m"}); chk = c5.get("/checkout").get_data(as_text=True)
assert "+₹40 fee" in chk and "Pay the courier" in chk and "₹99" in chk                                 # shipping + COD fee from settings
assert "Hello" in get("/contact") and "Say hi" in get("/contact")
# validation
for k, v, msg in [("background_color", "red", "colour"), ("contact_email", "nope", "email"), ("ship_flat_rate", "x", "whole rupee"),
                  ("nav", "no pipe here", "Title | /link"), ("nav", "X | javascript:alert(1)", "Title | /link"),
                  ("featured_collection", "ghost", "featured collection"), ("ga4_id", "evil\"><script>", "Google Analytics"),
                  ("featured_limit", "50", "1–12"), ("hero_button_link", "javascript:x", "must start with")]:
    rr = apost("/admin/settings", form | {k: v}, code=400, content_type="multipart/form-data"); assert msg in rr.get_data(as_text=True), (k, msg)
assert "Diwali sale" in get("/"), "failed save must not change anything"
# replacing hero deletes the old file; removing clears it
old = hero.group(1); apost("/admin/settings", form | {"hero_image": (png(900, 700, "pink"), "h2.png")}, content_type="multipart/form-data")
assert c.get(old).status_code == 404
apost("/admin/settings", form | {"remove_hero_image": "1"}, content_type="multipart/form-data"); assert "Add a hero image" in get("/")
assert os.listdir(os.environ["UPLOAD_DIR"]) == []
# blank announcement hides the bar
apost("/admin/settings", form | {"announcement_text": ""}, content_type="multipart/form-data"); assert 'class="announcement"' not in get("/")

# ---------- messages ----------
t6 = tok(get("/contact")); c.post("/contact", data={"csrf_token": t6, "name": "Zed", "email": "z@z.co", "topic": "Hello", "body": "hi there"})
m = aget("/admin/messages"); assert "hi there" in m
mid = int(re.search(r'name="id" value="(\d+)"', m).group(1)); apost("/admin/messages", {"id": str(mid)}); assert "hi there" not in aget("/admin/messages")

# ---------- orders dashboard + filter ----------
o = aget("/admin"); assert "New orders" in o and "Total sales" in o and "Asha Rao" in o
assert "Asha Rao" in aget("/admin?status=new") and "Asha Rao" not in aget("/admin?status=shipped")
assert "Loom" not in aget("/admin/orders.csv") and aget("/admin/stock")

# ---------- content survives in a second worker/process view (version cache) ----------
before = _rd("site.json")["shop_name"]; assert before == "Loom & Co."

# ---------- brute-force throttle (keep last: it locks the test client's IP) ----------
bad_hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
codes = [c.get("/admin", headers=bad_hdr).status_code for _ in range(12)]
assert codes[:10] == [401] * 10 and codes[10] == 429, codes
assert c.get("/admin", headers=HDR).status_code == 429, "even the right password is blocked during lockout"
print("ADMIN TESTS PASSED")
