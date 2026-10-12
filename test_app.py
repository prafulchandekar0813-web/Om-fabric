"""Smoke tests: python test_app.py   (no network or real keys needed)"""
import base64, hashlib, hmac, json, os, re, sqlite3, tempfile
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(tmp, "t.db"), ADMIN_PASSWORD="pw", SECRET_KEY="test",
                  RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="shh", RAZORPAY_WEBHOOK_SECRET="whsec",
                  SITE_URL="https://shop.example")
import app as A

sent = []                                   # captured emails
A.send_email = lambda to, subject, body, reply_to=None: sent.append((to, subject, body))
rz_calls = []
def fake_rz(amount, receipt, notes):
    rz_calls.append((amount, receipt)); return {"id": f"order_{receipt}"}
A.razorpay_create_order = fake_rz

c = A.app.test_client()
def tok(html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
def get(path, code=200, client=None):
    r = (client or c).get(path); assert r.status_code == code, (path, r.status_code); return r.get_data(as_text=True)
def post(path, data, client=None, **kw): return (client or c).post(path, data=data, **kw)
def stock(vid): return sqlite3.connect(os.environ["DATABASE_PATH"]).execute("SELECT stock FROM inventory WHERE variant_id=?", (vid,)).fetchone()[0]
HDR = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
GOOD = {"name": "Asha Rao", "email": "asha@example.com", "phone": "9876543210", "address": "12 Marine Drive",
        "city": "Mumbai", "state": "Maharashtra", "pincode": "400020", "payment": "cod", "notes": "<script>x</script>"}
def add(vid, qty=1, client=None):
    cl = client or c
    t = tok(get("/products/relaxed-linen-shirt", client=cl)); return post("/cart/add", {"csrf_token": t, "id": vid, "quantity": qty}, cl)
def checkout(extra=None, client=None):
    cl = client or c
    t = tok(get("/checkout", client=cl)); return post("/checkout", {**GOOD, "csrf_token": t, **(extra or {})}, cl)

# ---------- pages, SEO, search, sort ----------
for p in ["/", "/collections/all", "/collections/sale", "/products/relaxed-linen-shirt", "/products/canvas-tote",
          "/pages/about", "/pages/shipping", "/pages/returns", "/pages/privacy", "/pages/terms",
          "/contact", "/cart", "/track", "/search", "/healthz"]:
    get(p)
home = get("/")
assert "₹1,499" in home and "<s>₹1,999</s>" in home and "Less noise.<br>More you." in home
assert 'rel="canonical" href="https://shop.example/"' in home and 'property="og:title"' in home
prod = get("/products/relaxed-linen-shirt")
assert 'application/ld+json' in prod and '"availability": "https://schema.org/InStock"' in prod.replace('":"', '": "')
assert 'noindex' in get("/cart") and 'noindex' in get("/search")
assert "DRAFT" in get("/pages/privacy") and "DRAFT" not in get("/pages/about")
sm = get("/sitemap.xml"); assert "https://shop.example/products/canvas-tote" in sm and "/pages/terms" in sm
assert "Sitemap: https://shop.example/sitemap.xml" in get("/robots.txt")
assert "Linen Shirt" in get("/search?q=linen") and "Relaxed Linen" not in get("/search?q=zzzz")
prices = [int(x.replace(",", "")) for x in re.findall(r"<p>₹([\d,]+) ", get("/collections/all?sort=price-asc"))]
assert prices == sorted(prices) and prices[0] == 499, prices
assert get("/collections/all?sort=bogus")                                   # bad sort ignored
assert "Sold out" in get("/products/wide-leg-trouser")                      # XL has 0 stock
for p in ["/collections/nope", "/products/nope", "/pages/nope", "/order/1/thanks", "/pay/1"]:
    get(p, 404)

# ---------- CSRF ----------
assert post("/cart/add", {"id": "rls-m"}).status_code == 400

# ---------- stock ----------
assert stock("rls-m") == 12
add("rls-m", 2); assert "Bag (2)" in get("/cart")
post("/cart/add", {"csrf_token": tok(get("/cart")), "id": "wlt-xl"}); assert "Bag (2)" in get("/cart"), "sold out must not add"
add("rls-xl", 50)                                                           # only 4 in stock -> capped
assert "Bag (6)" in get("/cart"), get("/cart")[:0]
cart_html = get("/cart"); t = tok(cart_html)
post("/cart/update", {"csrf_token": t, "qty_rls-xl": "0"})

# ---------- discount codes ----------
t = tok(get("/cart"))
post("/cart/discount", {"csrf_token": t, "code": "nope"}); assert "isn’t valid" in get("/cart")
post("/cart/discount", {"csrf_token": t, "code": "welcome10"})
cart_html = get("/cart"); assert "Discount (WELCOME10)" in cart_html and "-₹300" in cart_html and "₹2,698" in cart_html, cart_html
post("/cart/update", {"csrf_token": t, "qty_rls-m": "1"})                    # now ₹1,499: 10% = 150, +80 shipping
cart_html = get("/cart"); assert "-₹150" in cart_html and "₹1,429" in cart_html

# ---------- COD checkout + emails ----------
r = checkout(); assert r.status_code == 302, r.get_data(as_text=True)[:400]
page = get(r.headers["Location"])
assert "Order #1001" in page and "Total: ₹1,429" in page and "-₹150" in page.replace("Discount (WELCOME10): ", "")
assert stock("rls-m") == 11, "stock reduced"
assert [s[0] for s in sent] == ["asha@example.com", "hello@example.com"], sent
assert "order #1001" in sent[0][1] and "Total: ₹1,429" in sent[0][2] and "https://shop.example/track" in sent[0][2]
assert "Bag (0)" in get("/cart")
assert A.app.test_client().get(r.headers["Location"]).status_code == 404
# once-per-customer code can't be reused by same email
c2 = A.app.test_client(); add("rls-m", client=c2)
t = tok(get("/cart", client=c2)); post("/cart/discount", {"csrf_token": t, "code": "WELCOME10"}, c2)
r2 = checkout(client=c2)                                                    # code applies at cart stage (no email yet)…
assert r2.status_code == 200 and "already used" in r2.get_data(as_text=True), "…but is rejected at checkout for a repeat customer"
# no oversell: someone else grabs the last stock between cart and checkout
with A.app.app_context():
    conn = sqlite3.connect(os.environ["DATABASE_PATH"]); conn.execute("UPDATE inventory SET stock=0 WHERE variant_id='rls-m'"); conn.commit()
r3 = checkout(client=c2); body = r3.get_data(as_text=True)
assert r3.status_code == 200 and ("sold out" in body.lower()), body[:200]
conn.execute("UPDATE inventory SET stock=11 WHERE variant_id='rls-m'"); conn.commit()

# ---------- Razorpay online payment ----------
c3 = A.app.test_client(); add("ect-m", 2, c3)                               # 2 x ₹799 = 1598 + 80 shipping
before = stock("ect-m")
r = checkout({"payment": "razorpay", "email": "buyer@example.com"}, c3)
assert r.status_code == 302 and "/pay/" in r.headers["Location"], r.get_data(as_text=True)[:300]
assert rz_calls[-1] == (1678, "TC-1002"), rz_calls
assert stock("ect-m") == before - 2, "stock held while awaiting payment"
paypage = get(r.headers["Location"], client=c3)
assert "Pay ₹1,678" in paypage and 'data-order="order_TC-1002"' in paypage and 'data-amount="167800"' in paypage
n_emails = len(sent)
t = tok(paypage)
bad = post("/pay/2/verify", {"csrf_token": t, "razorpay_order_id": "order_TC-1002", "razorpay_payment_id": "pay_1", "razorpay_signature": "forged"}, c3)
assert bad.status_code == 302 and "/pay/2" in bad.headers["Location"] and len(sent) == n_emails, "forged signature rejected"
assert "couldn’t verify" in get("/pay/2", client=c3)
sig = hmac.new(b"shh", b"order_TC-1002|pay_1", hashlib.sha256).hexdigest()
ok = post("/pay/2/verify", {"csrf_token": t, "razorpay_order_id": "order_TC-1002", "razorpay_payment_id": "pay_1", "razorpay_signature": sig}, c3)
assert ok.status_code == 302 and "/thanks" in ok.headers["Location"]
assert "Paid online" in get(ok.headers["Location"], client=c3)
assert len(sent) == n_emails + 2 and "Paid online" in sent[-1][1]
post("/pay/2/verify", {"csrf_token": t, "razorpay_order_id": "order_TC-1002", "razorpay_payment_id": "pay_1", "razorpay_signature": sig}, c3)
assert len(sent) == n_emails + 2, "replay must not re-send emails"

# ---------- webhook ----------
c4 = A.app.test_client(); add("ect-l", 1, c4); checkout({"payment": "razorpay", "email": "w@example.com"}, c4)   # order 1003 pending
body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_9", "order_id": "order_TC-1003"}}}}).encode()
assert c.post("/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": "bad"}).status_code == 400
good_sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
n = len(sent); assert c.post("/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": good_sig}).status_code == 200
assert len(sent) == n + 2
db = sqlite3.connect(os.environ["DATABASE_PATH"]); db.row_factory = sqlite3.Row
o = db.execute("SELECT status,payment_status FROM orders WHERE id=3").fetchone(); assert (o["status"], o["payment_status"]) == ("new", "paid")
c.post("/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": good_sig}); assert len(sent) == n + 2, "idempotent"

# ---------- unpaid hold expires and stock is released ----------
c5 = A.app.test_client(); add("ect-s", 3, c5); s_before = stock("ect-s")
checkout({"payment": "razorpay", "email": "slow@example.com"}, c5)          # order 1004
assert stock("ect-s") == s_before - 3
db.execute("UPDATE orders SET created_at='2000-01-01 00:00:00' WHERE id=4"); db.commit()
get("/")                                                                    # any page; inventory() runs the release
get("/products/everyday-cotton-tee")
assert stock("ect-s") == s_before and db.execute("SELECT payment_status FROM orders WHERE id=4").fetchone()[0] == "expired"
assert get("/pay/4", 410, client=c5) if False else c5.get("/pay/4").status_code == 410
# …and a late payment revives it
body = json.dumps({"event": "order.paid", "payload": {"payment": {"entity": {"id": "pay_late", "order_id": "order_TC-1004"}}}}).encode()
c.post("/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": hmac.new(b"whsec", body, hashlib.sha256).hexdigest()})
o = db.execute("SELECT status,payment_status,admin_note FROM orders WHERE id=4").fetchone()
assert (o["status"], o["payment_status"]) == ("new", "paid") and "check stock" in o["admin_note"]

# ---------- Razorpay API failure releases the hold ----------
def boom(*a): raise RuntimeError("network")
A.razorpay_create_order = boom
c6 = A.app.test_client(); add("ect-s", 1, c6); s0 = stock("ect-s")
r = checkout({"payment": "razorpay", "email": "x@example.com"}, c6)
assert r.status_code == 200 and "isn’t available right now" in r.get_data(as_text=True) and stock("ect-s") == s0
A.razorpay_create_order = fake_rz

# ---------- tracking ----------
t = tok(get("/track"))
assert "couldn’t find" in post("/track", {"csrf_token": t, "number": "1001", "email": "wrong@x.com"}).get_data(as_text=True)
assert "couldn’t find" in post("/track", {"csrf_token": t, "number": "9999", "email": "asha@example.com"}).get_data(as_text=True)
tr = post("/track", {"csrf_token": t, "number": "#1001", "email": "ASHA@example.com"}).get_data(as_text=True)
assert "Order #1001" in tr and "Order received" in tr

# ---------- admin ----------
assert c.get("/admin").status_code == 401
assert c.get("/admin", headers={"Authorization": "Basic " + base64.b64encode(b"admin:no").decode()}).status_code == 401
adm = c.get("/admin", headers=HDR).get_data(as_text=True)
assert "Asha Rao" in adm and "&lt;script&gt;x&lt;/script&gt;" in adm and "Online payments: <strong>ON" in adm
t = tok(adm)
n = len(sent)
post("/admin/order/1/status", {"csrf_token": t, "status": "shipped", "tracking": "Delhivery 123456"}, headers=HDR)
assert sent[-1][0] == "asha@example.com" and "has shipped" in sent[-1][1] and "Delhivery 123456" in sent[-1][2]
assert "Shipped" in post("/track", {"csrf_token": tok(get("/track")), "number": "1001", "email": "asha@example.com"}).get_data(as_text=True)
# cancelling restocks, and cancelled can't be reopened
s_now = stock("rls-m")
post("/admin/order/1/status", {"csrf_token": t, "status": "cancelled", "tracking": ""}, headers=HDR)
assert stock("rls-m") == s_now + 1 and "cancelled" in sent[-1][1]
post("/admin/order/1/status", {"csrf_token": t, "status": "new", "tracking": ""}, headers=HDR)
assert db.execute("SELECT status FROM orders WHERE id=1").fetchone()[0] == "cancelled"
# stock editor
post("/admin/stock", {"csrf_token": t, "stock_rls-m": "99", "stock_rls-s": "x"}, headers=HDR); assert stock("rls-m") == 99 and stock("rls-s") == 6
csv_out = c.get("/admin/orders.csv", headers=HDR).get_data(as_text=True); assert "Asha Rao" in csv_out and csv_out.startswith("order,")
bk = c.get("/admin/backup.db", headers=HDR); assert bk.status_code == 200 and bk.data.startswith(b"SQLite format 3")

# ---------- contact ----------
t = tok(get("/contact"))
assert post("/contact", {"csrf_token": t, "name": "Z", "email": "bad", "body": "hi"}).status_code == 400
assert post("/contact", {"csrf_token": t, "name": "Z", "email": "z@z.com", "topic": "Other", "body": "hello"}).status_code == 302
assert "Thanks for your message" in get("/contact?sent=1")
assert post("/contact", {"csrf_token": t, "website": "spam"}).status_code == 302
print("ALL TESTS PASSED")
