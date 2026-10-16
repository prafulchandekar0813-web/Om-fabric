# Thread & Co. — standalone store (Python · HTML · CSS · JS)

Your Shopify theme rebuilt as a normal website you can host anywhere that runs Python —
**now fully managed from a browser admin page** (`/admin`), no file editing needed.

## Run it on your computer
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
ADMIN_PASSWORD=choose-a-password python app.py         # Windows (PowerShell): $env:ADMIN_PASSWORD="choose-a-password"; python app.py
```
Open http://localhost:5000 for the store and http://localhost:5000/admin for the admin
(username **admin**, password = what you set). Checks: `python test_app.py && python test_admin.py && python test_accounts.py && python test_carts.py`.

## Running the shop from /admin
| Tab | What you can do |
|---|---|
| **Orders** | Dashboard (new orders, sales, low stock), filter by status, change status, add courier tracking (customer is emailed on *shipped*/*cancelled*), CSV export, database backup download |
| **Products** | **Add / edit / delete / duplicate** products. Title, description, brand, **photo upload** (several, pick the main one), sizes/options each with price, "was" price (sale), stock and sold-out switch, collections, and **Hide from store** (drafts) |
| **Collections** | Add, rename, describe and delete collections (Tops, Sale, Festive…) |
| **Discounts** | **Add, edit, pause and delete codes**: % off, ₹ off or free shipping; minimum order, expiry date, max uses, one-use-per-customer; shows how often each was used |
| **Pages** | Add/edit/delete pages (About, Shipping, Returns, Privacy, Terms, anything). Tick *Draft* to show a "please review" banner |
| **Stock** | Quick table to update counts for every counted item |
| **Messages** | Contact-form messages (delete when done) |
| **Customers** | Everyone with an account: search, see orders/spend, mark an email verified, delete an account (orders are kept, unlinked) |
| **Carts** | Abandoned-cart dashboard: bags tracked, reminders sent, sales recovered, a "ready to send?" checklist, and a *Run reminders now* button |
| **Settings** | Shop name, logo text, announcement bar, colours, header/footer menus, home-page banner (text + image), featured products section, shipping fee / free-shipping amount / COD fee, contact page text, business details (GSTIN etc.), Google Analytics / Plausible IDs |

Tips
- **Sizes:** one row per size. A product with no sizes: fill **one row** and leave its name blank.
- **Stock:** leave blank = "don't count" (always available). A number = counted: it drops with each order, comes back if you cancel, and customers see "only 3 left".
- **Sale:** put the current price in *Price* and the old price in *Was*.
- **Photos:** JPG/PNG/WebP/GIF up to 8 MB. They're automatically resized (max 1600 px), converted to WebP and stripped of hidden metadata such as GPS location.
- **Menus:** one link per line, `Title | /link` — e.g. `Sale | /collections/sale`, `Size guide | /pages/size-guide`.
- After deleting a collection or page, update menu links that pointed to it (Settings).

## Customer accounts (optional for shoppers)
Shoppers can still check out as **guests**. An account is optional and gives them: order history, tracking, one saved address that pre-fills checkout, a "keep me signed in" option, and password reset by email.
- **Sign up / log in / log out**, **forgot password** (single-use link, valid 1 hour), **change password** (signs out other devices), **edit details**, **delete account** (their details are erased; past orders stay in your records, unlinked).
- **Email confirmation:** a new account must confirm its email before it can see *guest orders placed with that address*. This stops someone signing up with another person's email to read their orders. Orders placed while logged in always show.
- **Security:** passwords are stored hashed (never in plain text), login/reset/sign-up attempts are rate-limited, reset links are single-use and expire, and there are no open redirects.
- **Needs email (SMTP)** to send the confirmation and reset emails. Without SMTP, nobody can reset a password; you can still *Mark verified* in **Admin → Customers**.
- Shoppers' header shows **Log in** / their first name. Links: `/account/login`, `/account/register`, `/account`.

## Abandoned-cart emails
When a shopper leaves items in their bag, we can email them a friendly reminder with a one-click link that restores the bag.
- **Only people who agreed are tracked**: account holders who keep "Email me a reminder if I leave items in my bag" on (it's an option at sign-up and in their profile), and guests who tick the (unticked-by-default) box at checkout. Nothing is tracked otherwise.
- **Rules (all configurable in Admin → Settings → Abandoned-cart emails):** first email after 2 hours, optional second after 24 hours (can include a discount code you choose), at most 2 per bag, at most 1 email per address every 72 hours, never between 10pm and 8am IST, stops as soon as they order or unsubscribe, ignores bags older than 7 days, and leaves out items that sold out.
- **Every email has an unsubscribe link** (and the one-click `List-Unsubscribe` header Gmail/Apple Mail use). Unsubscribing only stops reminders — receipts and password emails still arrive.
- **Make it run** (the Carts tab shows what's missing):
  1. Set up email: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, plus `SITE_URL`.
  2. Pick **one** way to run the job: **(a)** `ENABLE_SCHEDULER=1` (easiest — the app checks every 10 minutes); **(b)** set `CRON_SECRET` and have a cron service (e.g. cron-job.org) call `https://yourshop/tasks/abandoned-carts` every 10–15 min with header `X-Cron-Key: <secret>`; **(c)** schedule `python reminders.py` (PythonAnywhere Tasks, Render Cron Job, cron).
  3. Send yourself a test: tick the box at checkout with your own email, add an item, leave, set the first delay to 1 hour in Settings (or wait), then **Admin → Carts → Run reminders now**.
- **Be a good sender:** these are emails to people who asked for them; keep your business address in Settings → Business details (it's added to the email footer). Consider SPF/DKIM on your sending domain so emails don't land in spam.
- **Privacy page:** the starting Privacy text now mentions accounts and cart reminders. If you already edited/published your Privacy page, **add those two paragraphs** (see `data/pages.json`) and re-check them with a professional.

## Where your data lives
Products, collections, discounts, pages, settings, orders, messages and stock are all stored in **one SQLite file** (`DATABASE_PATH`). Uploaded photos are in `UPLOAD_DIR`.
The files in `data/*.json` are only the **starting content** used the first time the app runs — editing them later has no effect (use /admin). To start over, delete the database file and uploads folder.
**Back up both** the database and the uploads folder (see Backups).

## Before you launch — checklist
- [ ] In **/admin → Products**: replace the sample products and photos with yours
- [ ] **/admin → Settings**: shop name, contact email (currently `hello@example.com`), banner image, business details
- [ ] **/admin → Pages**: review the Shipping, Returns, Privacy and Terms drafts, edit for your business, **untick Draft**. They're starting points, not legal advice
- [ ] **/admin → Discounts**: delete or edit the sample codes (`WELCOME10`, `FLAT100`)
- [ ] Set environment variables: `SECRET_KEY`, `ADMIN_PASSWORD`, `SITE_URL`, `COOKIE_SECURE=1`, `TRUST_PROXY=1` (on hosted platforms), and point `DATABASE_PATH` + `UPLOAD_DIR` at a **persistent disk** (see `.env.example`)
- [ ] Set up emails (below) and place a test COD order end-to-end, on your phone too
- [ ] Create a test **customer account**, confirm the email, log in on a phone, reset the password once
- [ ] Turn on a way to run cart reminders (`ENABLE_SCHEDULER=1`) and send yourself one test reminder
- [ ] Add the **accounts + cart-reminder** paragraphs to your Privacy page if you edited it

## Emails
Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (and `NOTIFY_EMAIL` for your own alerts). Any SMTP provider works (Gmail app password, Brevo, Zoho, SES…). Without them, orders still work but no emails are sent. The admin Orders tab shows whether email is ON.

## Backups
Orders are your business records. Either download from **/admin → Orders → Download database backup**, or schedule `python backup.py` (daily) which keeps the newest 30 copies in `backups/`. Also copy your `uploads/` folder. Copy backups off the server.

## Deploy
Start command everywhere: `gunicorn app:app`
- **Render / Railway / Fly.io:** build `pip install -r requirements.txt`, start `gunicorn app:app` (a `Procfile` is included). Attach a **persistent disk**, set `DATABASE_PATH` and `UPLOAD_DIR` onto it, otherwise your products, photos and orders vanish when the app restarts. Set `TRUST_PROXY=1`.
- **PythonAnywhere:** upload the folder, create a Flask web app pointing at `app.py`, set env vars in the WSGI file.
- **VPS:** gunicorn behind nginx/Caddy for https, run under systemd.
- **Docker:** `docker build -t threadandco . && docker run -p 8000:8000 -v tc-data:/data --env-file .env threadandco`

Static-only hosts (GitHub Pages, Netlify drop, S3) won't work — the cart, checkout and admin need the Python server. Run **one** app instance on **one** machine (SQLite); that comfortably handles a small store.

## Security notes
Admin is protected by a password (HTTP login, so **only use it over https**), with login throttling (10 wrong tries → 15-minute lockout), CSRF protection on every form, escaped output, sanitised HTML in descriptions/pages (no scripts), and image re-encoding on upload. Use a long, unique `ADMIN_PASSWORD`.

## Online payments (switched off)
Razorpay support is built in but **dormant**: it only activates if you set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. Until then the store takes Cash on Delivery orders only. When you're ready, see the comments in `.env.example`; test with Razorpay *test* keys first.

## Not included
GST-formatted tax invoices, courier-API integration (Shiprocket etc.), multiple admin users, multiple saved addresses per customer, social login (Google/Facebook), SMS/WhatsApp reminders, and legal/tax advice.

## Layout
```
app.py            storefront, cart, discounts, checkout, tracking, SEO, payments (dormant)
admin.py          the whole admin panel
backup.py         database backup script
data/             starting content (first run only)
templates/        Jinja2 HTML (admin_*.html = admin)
static/           css/theme.css · js/ · img/
accounts.py       customer accounts (sign up, login, reset, profile)
carts.py          abandoned-cart tracking + reminder job
reminders.py      run the reminder job from cron
test_app.py  test_admin.py  test_accounts.py  test_carts.py   automated checks
```
