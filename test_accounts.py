"""Customer account tests: python test_accounts.py"""
import base64, os, re, sqlite3, tempfile
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"), ADMIN_PASSWORD="pw",
                  SECRET_KEY="test", SITE_URL="https://shop.example")
import app as A

sent = []
A.send_email = lambda to, subject, body, reply_to=None, html=None, headers=None: sent.append((to, subject, body))
HDR = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
PW = "Sup3rSecret!"
def tok(html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
def fresh(): A._throttle_hits.clear(); return A.app.test_client()
def get(cl, path, code=200):
    r = cl.get(path); assert r.status_code == code, (path, r.status_code); return r.get_data(as_text=True)
def post(cl, path, data, page="/account/login", code=302, **kw):
    t = tok(get(cl, page))
    r = cl.post(path, data={"csrf_token": t, **data}, **kw); assert r.status_code == code, (path, r.status_code, r.get_data(as_text=True)[:500]); return r
def sql(q, *a):
    conn = sqlite3.connect(os.environ["DATABASE_PATH"]); conn.row_factory = sqlite3.Row
    out = conn.execute(q, a).fetchall(); conn.commit(); conn.close(); return out
def mails(to, subject_part): return [m for m in sent if m[0] == to and subject_part in m[1]]
def register(cl, email, name="Asha Rao", pw=PW, code=302, **extra):
    d = {"name": name, "email": email, "phone": "", "password": pw, "password2": pw, "reminders": "1"}; d.update(extra)
    return post(cl, "/account/register", d, page="/account/register", code=code)
def login(cl, email, pw=PW, code=302, **extra):
    return post(cl, "/account/login", {"email": email, "password": pw, **extra}, code=code)
def buy(cl, email, vid="ect-m", **extra):
    t = tok(get(cl, "/products/everyday-cotton-tee")); cl.post("/cart/add", data={"csrf_token": t, "id": vid, "quantity": "1"})
    page = get(cl, "/checkout"); t = tok(page)
    d = {"csrf_token": t, "name": "Asha Rao", "email": email, "phone": "9876543210", "address": "12 Marine Drive", "city": "Mumbai",
         "state": "Maharashtra", "pincode": "400020", "payment": "cod"}; d.update(extra)
    r = cl.post("/checkout", data=d); assert r.status_code == 302, r.get_data(as_text=True)[:400]; return r.headers["Location"]
def link(mail_body, kind): return re.search(rf"/account/{kind}/([\w-]+)", mail_body).group(0)

# ---------- pages / protection ----------
cl = fresh()
for p in ["/account/login", "/account/register", "/account/forgot"]: get(cl, p)
r = cl.get("/account"); assert r.status_code == 302 and "/account/login?next=/account" in r.headers["Location"]
assert cl.get("/account/profile").status_code == 302 and cl.get("/account/orders/1001").status_code == 302
assert "Log in</a>" in get(cl, "/") and "noindex" in get(cl, "/account/login")

# ---------- registration validation ----------
bad = lambda **kw: register(cl, kw.pop("email", "new@example.com"), code=400, **kw).get_data(as_text=True)
assert "at least 8" in bad(pw="short1", password2="short1")
assert "too easy" in bad(pw="password123", password2="password123")
assert "don’t match" in bad(password2="Different123!")
assert "valid email" in bad(email="not-an-email")
assert "10-digit" in bad(phone="12345")
assert "name" in bad(name="A").lower()
assert sql("SELECT COUNT(*) c FROM customers")[0]["c"] == 0

# ---------- a guest order placed BEFORE the account exists ----------
guest = fresh(); guest_order_url = buy(guest, "Asha@Example.com")                 # order #1001, guest
thanks = get(guest, guest_order_url); assert "Create an account" in thanks and "/account/register?email=" in thanks

# ---------- register + verify ----------
asha = fresh()
r = register(asha, "  Asha@Example.com ")
assert r.headers["Location"].endswith("/account")
row = sql("SELECT * FROM customers")[0]
assert row["email"] == "asha@example.com" and row["email_verified"] == 0 and row["cart_reminders"] == 1
assert PW not in row["password_hash"] and row["password_hash"].split(":")[0] in ("scrypt", "pbkdf2"), row["password_hash"][:12]
vm = mails("asha@example.com", "Confirm your email"); assert len(vm) == 1
home = get(asha, "/account"); assert "Hi, Asha" in home and "Confirm your email" in home and "Asha</a>" in get(asha, "/")
assert "No orders yet" in home, "unverified email must NOT reveal earlier guest orders (privacy)"
assert get(asha, "/account/orders/1001", 404) is not None
register(fresh(), "asha@example.com", code=400)                                      # duplicate
assert "already exists" in register(fresh(), "ASHA@example.com", code=400).get_data(as_text=True)
vlink = link(vm[0][2], "verify")
assert asha.get(vlink).status_code == 200 and sql("SELECT email_verified FROM customers")[0][0] == 1
assert fresh().get(vlink).status_code == 410, "verification link is single-use"
home = get(asha, "/account"); assert "#1001" in home and "Confirm your email" not in home
assert "Order #1001" in get(asha, "/account/orders/1001")
# resend only for unverified; throttle
post(asha, "/account/resend-verification", {}, page="/account"); assert len(mails("asha@example.com", "Confirm your email")) == 1

# ---------- logged-in checkout: prefill, links to account, saves address ----------
page = (lambda cl_: (cl_.post("/cart/add", data={"csrf_token": tok(get(cl_, "/products/everyday-cotton-tee")), "id": "ect-l", "quantity": "1"}), get(cl_, "/checkout"))[1])(asha)
assert 'value="Asha Rao"' in page and 'value="asha@example.com"' in page and "Signed in as asha@example.com" in page and "save_address" in page
assert 'name="remind"' not in page, "logged-in users manage reminders in their profile"
t = tok(page)
r = asha.post("/checkout", data={"csrf_token": t, "name": "Asha Rao", "email": "asha@example.com", "phone": "9876543210", "address": "12 Marine Drive",
              "city": "Mumbai", "state": "Maharashtra", "pincode": "400020", "payment": "cod", "save_address": "1"}); assert r.status_code == 302
assert sql("SELECT customer_id FROM orders ORDER BY id DESC LIMIT 1")[0][0] == 1
c1 = sql("SELECT * FROM customers")[0]; assert (c1["address"], c1["pincode"], c1["phone"]) == ("12 Marine Drive", "400020", "9876543210")
asha.post("/cart/add", data={"csrf_token": tok(get(asha, "/products/everyday-cotton-tee")), "id": "ect-s", "quantity": "1"})
pf = get(asha, "/checkout"); assert "12 Marine Drive" in pf and 'value="400020"' in pf
assert "#1002" in get(asha, "/account") and "#1001" in get(asha, "/account")

# ---------- other people can't see my orders ----------
bob = fresh(); register(bob, "bob@example.com", name="Bob Das")
assert "No orders yet" in get(bob, "/account"); get(bob, "/account/orders/1001", 404); get(bob, "/account/orders/1002", 404)
get(bob, "/account/orders/5", 404); get(bob, "/account/orders/99999", 404)
# even if Bob "verifies" a different address, a guest order stays tied to its own email
guest2 = fresh(); buy(guest2, "someone.else@example.com")                          # order #1003
vb = link(mails("bob@example.com", "Confirm")[0][2], "verify"); bob.get(vb)
get(bob, "/account/orders/1003", 404)

# ---------- login / logout ----------
cl = fresh()
r = login(cl, "ASHA@example.com", pw="wrong", code=401); assert "don’t match" in r.get_data(as_text=True)
r2 = login(cl, "ghost@example.com", pw="wrong", code=401); assert "don’t match" in r2.get_data(as_text=True), "same message for unknown email"
assert login(cl, "asha@example.com").headers["Location"].endswith("/account")
assert cl.get("/account/login").status_code == 302                                                  # already logged in
r = post(cl, "/account/logout", {}, page="/account", code=302); assert cl.get("/account").status_code == 302
assert cl.post("/account/logout", data={}).status_code == 400                                       # CSRF enforced
# next= handling (no open redirects)
for nxt, dest in [("/cart", "/cart"), ("//evil.com", "/account"), ("https://evil.com", "/account"), ("/\\evil.com", "/account"), ("", "/account")]:
    c_ = fresh(); loc = login(c_, "asha@example.com", next=nxt).headers["Location"]; assert loc.endswith(dest) and "evil" not in loc, (nxt, loc)
c_ = fresh(); r = cl.get("/account/profile"); assert "/account/login?next=/account/profile" in r.headers["Location"]
# remember me -> persistent cookie
def cookie_hdr(r): return " ".join(r.headers.getlist("Set-Cookie")).lower()
c_ = fresh(); t = tok(get(c_, "/account/login"))
assert "expires=" not in cookie_hdr(c_.post("/account/login", data={"csrf_token": t, "email": "asha@example.com", "password": PW}))
c_ = fresh(); t = tok(get(c_, "/account/login"))
assert "expires=" in cookie_hdr(c_.post("/account/login", data={"csrf_token": t, "email": "asha@example.com", "password": PW, "remember": "1"}))
# brute-force protection per email
c_ = fresh()
codes = [c_.post("/account/login", data={"csrf_token": tok(get(c_, "/account/login")), "email": "bob@example.com", "password": "nope"}).status_code for i in range(10)]
assert codes[:8] == [401] * 8 and codes[8:] == [429, 429], codes
assert c_.post("/account/login", data={"csrf_token": tok(get(c_, "/account/login")), "email": "bob@example.com", "password": PW}).status_code == 429, "locked even for the right password"
A._throttle_hits.clear()

# ---------- forgot / reset ----------
cl = fresh(); n = len(sent)
r = post(cl, "/account/forgot", {"email": "ghost@example.com"}, page="/account/forgot", code=200)
assert "reset link is on its way" in r.get_data(as_text=True) and len(sent) == n, "no email for unknown address, same answer"
old_session = fresh(); login(old_session, "asha@example.com")                                  # another device, logged in
r = post(cl, "/account/forgot", {"email": "ASHA@example.com"}, page="/account/forgot", code=200)
assert "reset link is on its way" in r.get_data(as_text=True); rm = mails("asha@example.com", "Reset your password"); assert len(rm) == 1
rlink = link(rm[0][2], "reset"); get(cl, rlink)
tk = rlink.split("/")[-1]
assert "too easy" in post(cl, rlink, {"password": "password123", "password2": "password123"}, page=rlink, code=400).get_data(as_text=True)
assert "don’t match" in post(cl, rlink, {"password": "NewPassw0rd!", "password2": "x"}, page=rlink, code=400).get_data(as_text=True)
post(cl, rlink, {"password": "NewPassw0rd!", "password2": "NewPassw0rd!"}, page=rlink)
assert cl.get(rlink).status_code == 410, "reset link works once"
assert len(mails("asha@example.com", "password was changed")) == 1
assert old_session.get("/account").status_code == 302, "password reset signs out other devices"
login(fresh(), "asha@example.com", pw=PW, code=401); login(fresh(), "asha@example.com", pw="NewPassw0rd!")
# expired token
post(cl, "/account/forgot", {"email": "bob@example.com"}, page="/account/forgot", code=200)
bl = link(mails("bob@example.com", "Reset your password")[0][2], "reset")
sql("UPDATE auth_tokens SET expires_at='2000-01-01 00:00:00' WHERE purpose='reset'"); assert cl.get(bl).status_code == 410
# throttle on reset requests
for i in range(4): cl.post("/account/forgot", data={"csrf_token": tok(get(cl, "/account/forgot")), "email": "bob@example.com"})
assert len(mails("bob@example.com", "Reset your password")) == 3, "reset emails are rate-limited per address"

# ---------- profile ----------
PW = "NewPassw0rd!"; A._throttle_hits.clear()
cl = fresh(); login(cl, "asha@example.com", PW)
prof = lambda d, code=302: post(cl, "/account/profile", d, page="/account/profile", code=code)
prof({"action": "details", "name": "Asha R. Rao", "phone": "9123456789"}); c1 = sql("SELECT * FROM customers WHERE id=1")[0]; assert (c1["name"], c1["phone"]) == ("Asha R. Rao", "9123456789")
assert "10-digit" in prof({"action": "details", "name": "Asha", "phone": "123"}, 400).get_data(as_text=True)
prof({"action": "address", "address": "5 New Road, Bandra", "city": "Mumbai", "state": "MH", "pincode": "400050"}); assert sql("SELECT pincode FROM customers WHERE id=1")[0][0] == "400050"
assert "6-digit PIN" in prof({"action": "address", "address": "5 New Road", "city": "Mumbai", "state": "MH", "pincode": "12"}, 400).get_data(as_text=True)
prof({"action": "address", "address": "", "city": "", "state": "", "pincode": ""}); assert sql("SELECT address FROM customers WHERE id=1")[0][0] == ""
prof({"action": "reminders"}); assert sql("SELECT cart_reminders FROM customers WHERE id=1")[0][0] == 0
prof({"action": "reminders", "cart_reminders": "1"}); assert sql("SELECT cart_reminders FROM customers WHERE id=1")[0][0] == 1
other = fresh(); login(other, "asha@example.com", PW)
assert "isn’t right" in prof({"action": "password", "current_password": "bad", "new_password": "Another1234!", "new_password2": "Another1234!"}, 400).get_data(as_text=True)
prof({"action": "password", "current_password": PW, "new_password": "Another1234!", "new_password2": "Another1234!"})
assert cl.get("/account").status_code == 200, "this device stays signed in"; assert other.get("/account").status_code == 302, "other devices are signed out"
assert len(mails("asha@example.com", "password was changed")) == 2
PW = "Another1234!"

# ---------- delete account ----------
cl = fresh(); login(cl, "asha@example.com", PW)
post(cl, "/account/delete", {"password": "wrong"}, page="/account/profile"); assert sql("SELECT COUNT(*) FROM customers WHERE id=1")[0][0] == 1
post(cl, "/account/delete", {"password": PW}, page="/account/profile")
assert sql("SELECT COUNT(*) FROM customers WHERE email='asha@example.com'")[0][0] == 0
assert sql("SELECT COUNT(*) FROM orders WHERE id IN (1,2)")[0][0] == 2 and sql("SELECT COUNT(*) FROM orders WHERE customer_id=1")[0][0] == 0, "orders kept, unlinked"
assert cl.get("/account").status_code == 302; login(fresh(), "asha@example.com", PW, code=401)

# ---------- admin: customers tab ----------
A._throttle_hits.clear()
adm = A.app.test_client(); page = adm.get("/admin/customers", headers=HDR).get_data(as_text=True)
assert "bob@example.com" in page and "Bob Das" in page and "Verified" in page and "asha@example.com" not in page
t = tok(page); assert adm.get("/admin/customers?q=bob", headers=HDR).get_data(as_text=True).count("Bob Das") == 1
assert "Bob Das" not in adm.get("/admin/customers?q=zzz", headers=HDR).get_data(as_text=True)
dave = fresh(); register(dave, "dave@example.com", name="Dave Roy")
dave_id = sql("SELECT id FROM customers WHERE email='dave@example.com'")[0][0]
assert sql("SELECT email_verified FROM customers WHERE id=?", dave_id)[0][0] == 0
assert adm.post("/admin/customers", data={"csrf_token": t, "id": dave_id, "action": "verify"}, headers=HDR).status_code == 302
assert sql("SELECT email_verified FROM customers WHERE id=?", dave_id)[0][0] == 1
assert adm.post("/admin/customers", data={"csrf_token": t, "id": dave_id, "action": "delete"}, headers=HDR).status_code == 302
assert sql("SELECT COUNT(*) FROM customers WHERE id=?", dave_id)[0][0] == 0
assert adm.get("/admin/customers").status_code == 401

# ---------- guests can still check out without an account ----------
g2 = fresh(); u = buy(g2, "walkin@example.com"); assert "confirmed" in get(g2, u)
print("ACCOUNT TESTS PASSED")
