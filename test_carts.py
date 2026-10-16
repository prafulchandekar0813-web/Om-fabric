"""Abandoned-cart tests: python test_carts.py   (no network; the SMTP send is replaced by a recorder)"""
import base64, json, os, re, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(tmp, "t.db"), UPLOAD_DIR=os.path.join(tmp, "up"), ADMIN_PASSWORD="pw",
                  SECRET_KEY="test", SITE_URL="https://shop.example", SMTP_HOST="smtp.test", CRON_SECRET="cronsecret")
import app as A
import admin as ADM

outbox, authmail = [], []
def fake_now(to, subject, body, reply_to=None, html=None, headers=None):
    outbox.append(dict(to=to, subject=subject, body=body, reply_to=reply_to, html=html, headers=headers or {})); return fake_now.ok
fake_now.ok = True
A.send_email_now = fake_now
A.send_email = lambda to, subject, body, reply_to=None, html=None, headers=None: authmail.append((to, subject, body))
HDR = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)                    # 11:30 IST — a normal sending hour
fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")

def tok(html): return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
def fresh(): A._throttle_hits.clear(); return A.app.test_client()
def get(cl, path, code=200):
    r = cl.get(path); assert r.status_code == code, (path, r.status_code); return r.get_data(as_text=True)
def sql(q, *a):
    conn = sqlite3.connect(os.environ["DATABASE_PATH"]); conn.row_factory = sqlite3.Row
    out = conn.execute(q, a).fetchall(); conn.commit(); conn.close(); return out
def add(cl, vid, qty=1):
    t = tok(get(cl, "/products/everyday-cotton-tee")); return cl.post("/cart/add", data={"csrf_token": t, "id": vid, "quantity": str(qty)})
def remind(cl, email, consent="1", token=None):
    t = token or tok(get(cl, "/products/everyday-cotton-tee"))
    return cl.post("/cart/remind", data={"csrf_token": t, "email": email, "consent": consent})
def cart_row(email): rows = sql("SELECT * FROM carts WHERE email=? ORDER BY id DESC", email.lower()); return rows[0] if rows else None
def guest(email, items=(("ect-m", 2), ("rls-m", 1))):
    cl = fresh()
    for vid, q in items: add(cl, vid, q)
    assert remind(cl, email).get_json()["ok"] is True; return cl
def age(email, hours, last_hours_ago=None, now=NOW):
    sql("UPDATE carts SET updated_at=?, last_reminder_at=? WHERE email=? AND status='active'",
        fmt(now - timedelta(hours=hours)), fmt(now - timedelta(hours=last_hours_ago)) if last_hours_ago is not None else None, email.lower())
def run(now=NOW):
    with A.app.app_context(): return A._carts.run_job(now=now)
def set_cfg(**kw):
    with A.app.app_context():
        site = A.read_doc("site.json"); site["abandoned_cart"] = dict({"enabled": True, "first_after_hours": 2, "send_second": True, "second_after_hours": 24, "discount_code": ""}, **kw)
        A.write_doc("site.json", site)

# ================= 1. consent: nothing is tracked unless the shopper agrees =================
a = fresh(); add(a, "ect-m", 1)
assert sql("SELECT COUNT(*) FROM carts")[0][0] == 0, "no tracking without consent"
co = get(a, "/checkout"); box = re.search(r'<input[^>]*name="remind"[^>]*>', co).group(0); assert "checked" not in box, "reminder box is UNticked by default"
assert remind(fresh(), "x@example.com").get_json() == {"ok": False}, "empty bag: nothing to remind about"
assert a.post("/cart/remind", data={"email": "g1@example.com", "consent": "1"}).status_code == 400, "CSRF enforced"
assert remind(a, "not-an-email").get_json()["ok"] is False and sql("SELECT COUNT(*) FROM carts")[0][0] == 0
assert remind(a, "G1@Example.com").get_json()["ok"] is True
r = cart_row("g1@example.com"); assert r["status"] == "active" and json.loads(r["items_json"]) == {"ect-m": 1} and r["subtotal"] == 799 and r["customer_id"] is None
# bag changes are mirrored; emptying -> 'emptied'; adding again restarts cleanly
add(a, "ect-l", 2); assert cart_row("g1@example.com")["subtotal"] == 799 + 1598
t = tok(get(a, "/cart")); a.post("/cart/update", data={"csrf_token": t, "qty_ect-m": "0", "qty_ect-l": "0"}); assert cart_row("g1@example.com")["status"] == "emptied"
sql("UPDATE carts SET reminders_sent=1 WHERE email='g1@example.com'"); add(a, "ect-s", 1)
r = cart_row("g1@example.com"); assert r["status"] == "active" and r["reminders_sent"] == 0 and r["subtotal"] == 799
assert sql("SELECT COUNT(*) FROM carts")[0][0] == 1, "same bag = same row"
# un-ticking withdraws consent and stops tracking
assert remind(a, "g1@example.com", consent="0").get_json()["ok"] is True; assert cart_row("g1@example.com")["status"] == "unsubscribed"
add(a, "ect-l", 1); r = cart_row("g1@example.com"); assert r["status"] == "unsubscribed" and json.loads(r["items_json"]) == {"ect-s": 1}, "stays untracked"
# no-JS fallback: ticking the box on the checkout form is enough, even if the form has errors
b = fresh(); add(b, "ect-m", 1); t = tok(get(b, "/checkout"))
r = b.post("/checkout", data={"csrf_token": t, "remind": "1", "email": "nojs@example.com", "name": "N", "phone": "bad"}); assert r.status_code == 200
assert cart_row("nojs@example.com")["status"] == "active"
# placing the order ends the "abandoned" state
t = tok(get(b, "/checkout")); r = b.post("/checkout", data={"csrf_token": t, "remind": "1", "name": "Nina Shah", "email": "nojs@example.com", "phone": "9876543210",
    "address": "12 Marine Drive", "city": "Mumbai", "state": "MH", "pincode": "400020", "payment": "cod"}); assert r.status_code == 302
r = cart_row("nojs@example.com"); assert r["status"] == "recovered" and r["recovered_order_id"] is not None
add(b, "ect-m", 1); assert cart_row("nojs@example.com")["status"] == "recovered" and sql("SELECT COUNT(*) FROM carts WHERE email='nojs@example.com'")[0][0] == 1, \
    "the new bag after an order is not tracked until they opt in again"
# rate-limit abuse of the consent endpoint
c9 = fresh(); add(c9, "ect-m"); t = tok(get(c9, "/products/everyday-cotton-tee"))
codes = [c9.post("/cart/remind", data={"csrf_token": t, "email": f"spam{i}@example.com", "consent": "1"}).status_code for i in range(32)]
assert codes[:30] == [200] * 30 and codes[30:] == [429, 429], codes

# ================= 2. accounts: preference decides =================
def signup(email, name, reminders="1"):
    cl = fresh(); t = tok(get(cl, "/account/register"))
    d = {"csrf_token": t, "name": name, "email": email, "phone": "", "password": "Sup3rSecret!", "password2": "Sup3rSecret!"}
    if reminders: d["reminders"] = reminders
    assert cl.post("/account/register", data=d).status_code == 302; return cl
mia = signup("mia@example.com", "Mia Kapoor"); add(mia, "ect-m", 2)
r = cart_row("mia@example.com"); assert r and r["customer_id"] is not None and r["status"] == "active", "opted-in account holders are tracked"
# cross-device: logging in elsewhere brings the bag back
dev2 = fresh(); t = tok(get(dev2, "/account/login")); dev2.post("/account/login", data={"csrf_token": t, "email": "mia@example.com", "password": "Sup3rSecret!"})
assert "Bag (2)" in get(dev2, "/cart")
# profile toggle
def pref(cl, on):
    t = tok(get(cl, "/account/profile")); d = {"csrf_token": t, "action": "reminders"}; d.update({"cart_reminders": "1"} if on else {})
    assert cl.post("/account/profile", data=d).status_code == 302
pref(mia, False); assert cart_row("mia@example.com")["status"] == "unsubscribed"; add(mia, "ect-l", 1)
assert json.loads(cart_row("mia@example.com")["items_json"]) == {"ect-m": 2}, "switched off -> not tracked"
pref(mia, True); r = cart_row("mia@example.com"); assert r["status"] == "active" and json.loads(r["items_json"]) == {"ect-m": 2, "ect-l": 1}
# regression (found in a real browser): guest ticks then unticks, THEN creates an account -> the account's bag is tracked
gx = fresh(); add(gx, "ect-m", 1); remind(gx, "priya.guest@example.com"); remind(gx, "priya.guest@example.com", consent="0")
t_ = tok(get(gx, "/account/register")); gx.post("/account/register", data={"csrf_token": t_, "name": "Priya Nair", "email": "priya@example.com", "phone": "",
    "password": "Sup3rSecret!", "password2": "Sup3rSecret!", "reminders": "1"})
r = cart_row("priya@example.com"); assert r and r["status"] == "active" and json.loads(r["items_json"]) == {"ect-m": 1}
assert cart_row("priya.guest@example.com")["status"] == "unsubscribed", "the guest opt-out stays respected"
# no consent at sign-up -> never tracked; logging out stops tracking
ned = signup("ned@example.com", "Ned Rao", reminders=""); add(ned, "ect-m"); assert cart_row("ned@example.com") is None
t = tok(get(mia, "/cart")); mia.post("/account/logout", data={"csrf_token": t}); add(mia, "ect-s", 1)
assert json.loads(cart_row("mia@example.com")["items_json"]) == {"ect-m": 2, "ect-l": 1}, "logged-out visitor isn't tracked as Mia"

# ================= 3. the reminder job: gating =================
sql("DELETE FROM carts"); sql("DELETE FROM email_suppressions")
c1 = guest("job@example.com"); age("job@example.com", 3)
set_cfg(enabled=False); assert "switched off" in run()["skipped"] and not outbox
set_cfg(); os.environ.pop("SMTP_HOST"); assert "Email isn’t set up" in run()["skipped"]; os.environ["SMTP_HOST"] = "smtp.test"
url = os.environ.pop("SITE_URL"); assert "SITE_URL" in run()["skipped"]; os.environ["SITE_URL"] = url
assert "Quiet hours" in run(NOW.replace(hour=17, minute=0))["skipped"], "10:30pm IST"
assert "Quiet hours" in run(NOW.replace(hour=21, minute=0))["skipped"], "2:30am IST"
assert "Quiet hours" in run(NOW.replace(hour=2, minute=29))["skipped"], "7:59am IST"
assert "skipped" not in run(NOW.replace(hour=2, minute=30)), "8:00am IST is fine to send"
outbox.clear(); sql("UPDATE carts SET reminders_sent=0, last_reminder_at=NULL, status='active' WHERE email='job@example.com'"); age("job@example.com", 3)
age("job@example.com", 1); assert run()["sent"] == 0 and not outbox, "too early (first reminder is after 2h)"

# ================= 4. first + second reminder =================
set_cfg(discount_code="WELCOME10"); age("job@example.com", 3)
res = run(); assert res["sent"] == 1, res; m = outbox[-1]
assert m["to"] == "job@example.com" and m["subject"] == "You left something in your bag at Thread & Co." and m["reply_to"] == "hello@example.com"
row = cart_row("job@example.com"); rt = row["restore_token"]
assert "  2 x Everyday Cotton Tee (M) — ₹1,598" in m["body"] and "  1 x Relaxed Linen Shirt (M) — ₹1,499" in m["body"] and "Subtotal: ₹3,097" in m["body"]
assert f"https://shop.example/cart/restore/{rt}" in m["body"] and "Unsubscribe: https://shop.example/unsubscribe/" in m["body"] and "WELCOME10" not in m["body"], "no code in the first email"
assert "Return to your bag" in m["html"] and f"https://shop.example/cart/restore/{rt}" in m["html"] and "https://shop.example/static/img/products/everyday-cotton-tee.svg" in m["html"]
assert m["headers"]["List-Unsubscribe"].startswith("<https://shop.example/unsubscribe/") and m["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
assert row["reminders_sent"] == 1 and run()["sent"] == 0, "not again straight away"
age("job@example.com", 26, last_hours_ago=1); assert run()["sent"] == 0, "second reminder waits 12h after the first"
age("job@example.com", 26, last_hours_ago=25); res = run(); assert res["sent"] == 1, res; m = outbox[-1]
assert m["subject"].startswith("Still thinking it over?") and "Use code WELCOME10 at checkout for 10% off orders over ₹499" in m["body"] and "WELCOME10" in m["html"]
assert cart_row("job@example.com")["reminder_code"] == "WELCOME10" and cart_row("job@example.com")["reminders_sent"] == 2
age("job@example.com", 60, last_hours_ago=40); assert run()["sent"] == 0 and len(outbox) == 2, "never more than two per bag"

# ================= 5. safety rules =================
outbox.clear()
# only one send even if the job runs twice at once (the second run starts while the first is "sending")
c2 = guest("race@example.com"); age("race@example.com", 3); inner = []
def reentrant(*a, **k):
    inner.append(run()); return fake_now(*a, **k)
A.send_email_now = reentrant; res = run(); A.send_email_now = fake_now
assert res["sent"] == 1 and inner[0]["sent"] == 0 and len([m for m in outbox if m["to"] == "race@example.com"]) == 1, (res, inner)
# failed sends: retried, then abandoned after 3 tries, and the bag isn't marked as reminded
c3 = guest("fail@example.com"); age("fail@example.com", 3); fake_now.ok = False
for _ in range(5): run()
r = cart_row("fail@example.com"); assert (r["reminders_sent"], r["fail_count"]) == (0, 3), dict(r)
fake_now.ok = True; n = len(outbox); run(); assert len(outbox) == n, "gave up after 3 failures"
# already ordered -> counted as recovered, no email
outbox.clear(); c4 = guest("bought@example.com"); age("bought@example.com", 3)
sql("INSERT INTO orders (name,email,phone,address,city,state,pincode,payment_method,items_json,subtotal,shipping,total,status,created_at) VALUES "
    "('B','bought@example.com','9876543210','addr line','City','ST','400001','COD','[]',100,0,100,'new',?)", fmt(NOW - timedelta(hours=1)))
res = run(); assert res["recovered"] == 1 and not outbox and cart_row("bought@example.com")["status"] == "recovered"
# a cancelled order does not count
c5 = guest("cancelled@example.com"); age("cancelled@example.com", 3)
sql("INSERT INTO orders (name,email,phone,address,city,state,pincode,payment_method,items_json,subtotal,shipping,total,status,created_at) VALUES "
    "('B','cancelled@example.com','9876543210','addr line','City','ST','400001','COD','[]',100,0,100,'cancelled',?)", fmt(NOW - timedelta(hours=1)))
assert run()["sent"] == 1
# frequency cap: one reminder per address per 72h across all bags
outbox.clear(); ca = guest("cap@example.com"); age("cap@example.com", 3); assert run()["sent"] == 1
cb = guest("cap@example.com", items=(("ect-l", 1),)); sql("UPDATE carts SET updated_at=? WHERE id=(SELECT MAX(id) FROM carts WHERE email='cap@example.com')", fmt(NOW - timedelta(hours=3)))
assert run()["sent"] == 0, "second bag within 72h is held back"
cap_mails = lambda: [m for m in outbox if m["to"] == "cap@example.com"]
later = NOW + timedelta(hours=73); sql("UPDATE carts SET updated_at=? WHERE id=(SELECT MAX(id) FROM carts WHERE email='cap@example.com')", fmt(later - timedelta(hours=3)))
outbox.clear(); run(later)
assert len(cap_mails()) == 1 and cap_mails()[0]["subject"].startswith("Still thinking"), "after 72h the FIRST bag gets its 2nd reminder; the other bag still waits (one email per address per window)"
assert [r["reminders_sent"] for r in sql("SELECT reminders_sent FROM carts WHERE email='cap@example.com' ORDER BY id")] == [2, 0]
later2 = later + timedelta(hours=73); sql("UPDATE carts SET updated_at=? WHERE email='cap@example.com' AND reminders_sent=0", fmt(later2 - timedelta(hours=3)))
outbox.clear(); run(later2)
assert len(cap_mails()) == 1 and cap_mails()[0]["subject"].startswith("You left something"), "another 72h on, the second bag's first reminder goes out"
# stale bags are retired, never emailed
outbox.clear(); cs = guest("stale@example.com"); age("stale@example.com", 24 * 8); res = run(); assert res["retired"] >= 1 and not [m for m in outbox if m["to"] == "stale@example.com"]
assert cart_row("stale@example.com")["status"] == "expired"
# sold-out items are left out; if nothing is left there's no email at all
sql("UPDATE inventory SET stock=0 WHERE variant_id='osj-xl'") if sql("SELECT 1 FROM inventory WHERE variant_id='osj-xl'") else get(fresh(), "/products/overshirt-jacket")
sql("UPDATE inventory SET stock=3 WHERE variant_id='osj-xl'")
outbox.clear(); cu = guest("partial@example.com", items=(("ect-m", 1), ("osj-xl", 1))); age("partial@example.com", 3)
sql("UPDATE inventory SET stock=0 WHERE variant_id='osj-xl'"); assert run()["sent"] == 1
assert "Overshirt" not in outbox[-1]["body"] and "Everyday Cotton Tee" in outbox[-1]["body"]
outbox.clear(); sql("UPDATE inventory SET stock=3 WHERE variant_id='osj-xl'"); cz = guest("gone@example.com", items=(("osj-xl", 1),)); age("gone@example.com", 3)
sql("UPDATE inventory SET stock=0 WHERE variant_id='osj-xl'"); res = run(); assert not outbox and cart_row("gone@example.com")["status"] == "expired"
sql("UPDATE inventory SET stock=3 WHERE variant_id='osj-xl'")
# product names are escaped in the HTML email
with A.app.app_context():
    doc = A.read_doc("products.json"); p = json.loads(json.dumps(doc["products"][1])); p.update(handle="xss-test", title='<img src=x onerror=alert(1)>Tee')
    for v in p["variants"]: v["id"] = "xss-" + v["id"]
    doc["products"].append(p); A.write_doc("products.json", doc)
outbox.clear(); cx = guest("xss@example.com", items=(("xss-ect-m", 1),)); age("xss@example.com", 3); assert run()["sent"] == 1
assert "&lt;img src=x onerror=alert(1)&gt;Tee" in outbox[-1]["html"] and "<img src=x onerror" not in outbox[-1]["html"]
# one-reminder mode
set_cfg(send_second=False); outbox.clear(); co_ = guest("once@example.com"); age("once@example.com", 3); run(); age("once@example.com", 40, last_hours_ago=30); run()
assert len([m for m in outbox if m["to"] == "once@example.com"]) == 1
set_cfg()

# ================= 6. restore link =================
set_cfg(discount_code="WELCOME10"); outbox.clear()
r1 = guest("back@example.com"); age("back@example.com", 3); run(); age("back@example.com", 26, last_hours_ago=25); run()
rt = cart_row("back@example.com")["restore_token"]; assert cart_row("back@example.com")["reminder_code"] == "WELCOME10"
ph = fresh(); r = ph.get(f"/cart/restore/{rt}"); assert r.status_code == 302 and r.headers["Location"].endswith("/cart")
cart = get(ph, "/cart"); assert "Bag (3)" in cart and "Discount (WELCOME10)" in cart and "Welcome back" in cart
assert cart_row("back@example.com")["clicked_at"] is not None
add(ph, "ect-s", 1); assert json.loads(cart_row("back@example.com")["items_json"]).get("ect-s") == 1 and sql("SELECT COUNT(*) FROM carts WHERE email='back@example.com'")[0][0] == 1, "keeps updating the same bag"
bad = fresh(); r = bad.get("/cart/restore/not-a-real-token"); pg = get(bad, "/cart"); assert r.headers["Location"].endswith("/cart") and "Bag (0)" in pg and "expired" in pg
sql("UPDATE carts SET status='recovered' WHERE email='back@example.com'"); done = fresh(); done.get(f"/cart/restore/{rt}"); assert "Bag (0)" in get(done, "/cart")
# someone else's account is not taken over by clicking a link
sql("UPDATE carts SET status='active' WHERE email='back@example.com'"); mia2 = fresh(); t = tok(get(mia2, "/account/login"))
mia2.post("/account/login", data={"csrf_token": t, "email": "mia@example.com", "password": "Sup3rSecret!"}); mia2.get(f"/cart/restore/{rt}"); add(mia2, "ect-s", 1)
assert cart_row("back@example.com")["email"] == "back@example.com" and "ect-s" in json.loads(cart_row("mia@example.com")["items_json"])
set_cfg()

# ================= 7. unsubscribe =================
with A.app.app_context(), A.app.test_request_context("/"): u = A._carts.unsubscribe_link("https://shop.example", "Unsub@Example.com")
path = u.replace("https://shop.example", ""); un = guest("unsub@example.com"); age("unsub@example.com", 3)
anon = fresh(); page = get(anon, path); assert "unsub@example.com" in page and "Yes, unsubscribe" in page and not sql("SELECT * FROM email_suppressions"), "GET alone changes nothing (mail scanners)"
assert fresh().get(path[:-3] + "xyz").status_code == 404, "tampered token rejected"
assert fresh().get("/unsubscribe/garbage").status_code == 404
r = anon.post(path, data={}); assert r.status_code == 200 and "unsubscribed" in r.get_data(as_text=True), "works without a session/CSRF (one-click from mail apps)"
r = anon.post(path, data={"List-Unsubscribe": "One-Click"}); assert r.status_code == 200
assert sql("SELECT email FROM email_suppressions")[0][0] == "unsub@example.com" and cart_row("unsub@example.com")["status"] == "unsubscribed"
outbox.clear(); assert run()["sent"] == 0 and not outbox
add(un, "ect-s", 1); assert cart_row("unsub@example.com")["status"] == "unsubscribed", "an old session is not re-tracked after unsubscribing"
# explicit new consent (ticking the box again) lifts it; an account's flag is switched off by the link
assert remind(un, "unsub@example.com").get_json()["ok"] is True and not sql("SELECT * FROM email_suppressions") and cart_row("unsub@example.com")["status"] == "active"
with A.app.app_context(), A.app.test_request_context("/"): u2 = A._carts.unsubscribe_link("https://shop.example", "mia@example.com")
fresh().post(u2.replace("https://shop.example", ""), data={}); assert sql("SELECT cart_reminders FROM customers WHERE email='mia@example.com'")[0][0] == 0
sql("DELETE FROM email_suppressions")

# ================= 8. scheduler endpoint + CLI =================
anon = fresh(); assert anon.get("/tasks/abandoned-carts").status_code == 403 and anon.get("/tasks/abandoned-carts?key=nope").status_code == 403
ok = anon.get("/tasks/abandoned-carts", headers={"X-Cron-Key": "cronsecret"}); assert ok.status_code == 200 and "sent" in ok.get_json()
assert anon.post("/tasks/abandoned-carts", data={"key": "cronsecret"}).status_code == 200, "POST works without CSRF"
assert anon.get("/tasks/abandoned-carts?key=cronsecret").status_code == 200
s = os.environ.pop("CRON_SECRET"); assert fresh().get("/tasks/abandoned-carts?key=cronsecret").status_code == 404; os.environ["CRON_SECRET"] = s
env = dict(os.environ, DATABASE_PATH=os.path.join(tmp, "cli.db"), UPLOAD_DIR=os.path.join(tmp, "cliup")); env.pop("SMTP_HOST", None)
out = subprocess.run([sys.executable, "reminders.py"], env=env, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
assert out.returncode == 0 and "skipped" in out.stdout, (out.stdout, out.stderr[-400:])
with A.app.app_context():
    site = A.read_doc("site.json"); site.pop("abandoned_cart"); A.write_doc("site.json", site)
    cfg = A._carts.config(); assert (cfg["enabled"], cfg["first_after_hours"], cfg["second_after_hours"], cfg["send_second"]) == (True, 2, 24, True), "defaults when the setting is missing"
set_cfg()

# ================= 9. admin =================
adm = A.app.test_client(); page = adm.get("/admin/carts", headers=HDR).get_data(as_text=True)
assert "Abandoned carts" in page and "Reminder emails sent" in page and "ready to send" in page and ">Carts</a>" in page and "back@example.com" in page
assert "built-in scheduler is ON" not in page and "cron endpoint is enabled" in page
assert adm.get("/admin/carts").status_code == 401
t = tok(page); r = adm.post("/admin/carts/run", data={"csrf_token": t}, headers=HDR); assert r.status_code == 302
assert "Reminder run finished" in adm.get("/admin/carts", headers=HDR).get_data(as_text=True)
os.environ.pop("SMTP_HOST"); adm.post("/admin/carts/run", data={"csrf_token": t}, headers=HDR); page = adm.get("/admin/carts", headers=HDR).get_data(as_text=True)
assert "Nothing sent — Email isn’t set up" in page and "NOT set up — set SMTP_HOST" in page; os.environ["SMTP_HOST"] = "smtp.test"
# recovery stats: reminded + recovered + revenue
st = None
with A.app.app_context(): st = A._carts.stats()
assert st["emails_sent"] >= 4 and st["reminded"] >= 3 and "revenue" in st
# settings validation + saving
with A.app.app_context(): base = ADM.site_to_form(A.read_doc("site.json"))
sp = lambda **kw: adm.post("/admin/settings", data={"csrf_token": t, **{k: v for k, v in dict(base, **kw).items() if v != ""}}, headers=HDR, content_type="multipart/form-data")
assert sp(ac_first="2", ac_second="24", ac_enabled="1", ac_second_on="1").status_code == 302
r = sp(ac_first="30", ac_second="24", ac_enabled="1", ac_second_on="1"); assert r.status_code == 400 and "must be later" in r.get_data(as_text=True)
r = sp(ac_first="x", ac_second="24", ac_enabled="1"); assert r.status_code == 400 and "whole hours" in r.get_data(as_text=True)
r = sp(ac_first="2", ac_second="24", ac_code="NOPE", ac_enabled="1"); assert r.status_code == 400 and "from the list" in r.get_data(as_text=True)
assert sp(ac_first="3", ac_second="30", ac_code="FLAT100", ac_enabled="1", ac_second_on="1").status_code == 302
with A.app.app_context(): saved = A.read_doc("site.json")["abandoned_cart"]
assert saved == {"enabled": True, "first_after_hours": 3, "send_second": True, "second_after_hours": 30, "discount_code": "FLAT100"}, saved
assert sp(ac_first="3", ac_second="30", ac_enabled="", ac_second_on="").status_code == 302                                    # boxes unticked -> off
with A.app.app_context(): saved = A.read_doc("site.json")["abandoned_cart"]
assert saved["enabled"] is False and saved["send_second"] is False
page = adm.get("/admin/settings", headers=HDR).get_data(as_text=True); assert "Abandoned-cart emails" in page and "Discount code in the second email" in page
print("CART TESTS PASSED")
