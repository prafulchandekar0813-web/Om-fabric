"""Smoke tests: python test_app.py"""
import os, re, tempfile
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ADMIN_PASSWORD"] = "pw"
os.environ["SECRET_KEY"] = "test"
import base64
from app import app

c = app.test_client()
def tok(html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

def get(path, code=200):
    r = c.get(path); assert r.status_code == code, (path, r.status_code); return r.get_data(as_text=True)

for p in ["/", "/collections/all", "/collections/sale", "/collections/new-arrivals", "/products/relaxed-linen-shirt",
          "/products/canvas-tote", "/pages/about", "/contact", "/cart", "/healthz", "/robots.txt"]:
    get(p)
for p in ["/collections/nope", "/products/nope", "/pages/nope", "/order/1/thanks", "/checkout"]:
    c.get(p).status_code in (404, 302) or (_ for _ in ()).throw(AssertionError(p))
assert c.get("/checkout").status_code == 302

h = get("/")
assert "₹1,499" in h and "<s>₹1,999</s>" in h and "Less noise.<br>More you." in h
assert "Sale</b>" in get("/collections/sale")
assert "(Sold out)" in get("/products/wide-leg-trouser")

# CSRF is enforced
assert c.post("/cart/add", data={"id": "rls-m"}).status_code == 400
# add to cart
t = tok(get("/products/relaxed-linen-shirt"))
assert c.post("/cart/add", data={"csrf_token": t, "id": "rls-m", "quantity": "2"}).status_code == 302
assert c.post("/cart/add", data={"csrf_token": t, "id": "wlt-xl"}).status_code == 302  # sold out -> flash, not added
assert c.post("/cart/add", data={"csrf_token": t, "id": "bogus"}).status_code == 404
cart = get("/cart")
assert "Bag (2)" in cart and "₹2,998" in cart and "Free" in cart, "free shipping over 1999"
# update qty and remove
t = tok(cart)
c.post("/cart/update", data={"csrf_token": t, "qty_rls-m": "1"})
assert "₹1,499" in get("/cart") and "₹80" in get("/cart")   # below free threshold -> flat rate
# checkout validation
t = tok(get("/checkout"))
r = c.post("/checkout", data={"csrf_token": t, "name": "A", "phone": "123", "payment": "cod"})
assert r.status_code == 200 and "valid email" in r.get_data(as_text=True) and "10-digit" in r.get_data(as_text=True)
good = {"csrf_token": t, "name": "Asha Rao", "email": "asha@example.com", "phone": "9876543210",
        "address": "12 Marine Drive", "city": "Mumbai", "state": "Maharashtra", "pincode": "400020",
        "payment": "cod", "notes": "<script>x</script>"}
r = c.post("/checkout", data=good); assert r.status_code == 302, r.get_data(as_text=True)[:300]
page = get(r.headers["Location"])
assert "Order #1001" in page and "Total: ₹1,579" in page
assert "Bag (0)" in get("/cart")
# thanks page not visible to another visitor
assert app.test_client().get(r.headers["Location"]).status_code == 404
# contact form
t = tok(get("/contact"))
assert c.post("/contact", data={"csrf_token": t, "name": "Z", "email": "bad", "body": "hi"}).status_code == 400
assert c.post("/contact", data={"csrf_token": t, "name": "Z", "email": "z@z.com", "topic": "Other", "body": "hello"}).status_code == 302
assert "Thanks for your message" in get("/contact?sent=1")
assert c.post("/contact", data={"csrf_token": t, "website": "spam"}).status_code == 302  # honeypot
# admin
assert c.get("/admin").status_code == 401
hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
a = c.get("/admin", headers=hdr); assert a.status_code == 200
txt = a.get_data(as_text=True)
assert "Asha Rao" in txt and "hello" in txt and "&lt;script&gt;x&lt;/script&gt;" in txt, "notes must be escaped"
assert c.get("/admin", headers={"Authorization": "Basic " + base64.b64encode(b"admin:no").decode()}).status_code == 401
t = tok(txt)
assert c.post("/admin/order/1/status", data={"csrf_token": t, "status": "shipped"}, headers=hdr).status_code == 302
assert "selected>shipped" in c.get("/admin", headers=hdr).get_data(as_text=True)
print("ALL TESTS PASSED")
