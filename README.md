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
(username **admin**, password = what you set). Checks: `python test_app.py && python test_admin.py`.

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
| **Settings** | Shop name, logo text, announcement bar, colours, header/footer menus, home-page banner (text + image), featured products section, shipping fee / free-shipping amount / COD fee, contact page text, business details (GSTIN etc.), Google Analytics / Plausible IDs |

Tips
- **Sizes:** one row per size. A product with no sizes: fill **one row** and leave its name blank.
- **Stock:** leave blank = "don't count" (always available). A number = counted: it drops with each order, comes back if you cancel, and customers see "only 3 left".
- **Sale:** put the current price in *Price* and the old price in *Was*.
- **Photos:** JPG/PNG/WebP/GIF up to 8 MB. They're automatically resized (max 1600 px), converted to WebP and stripped of hidden metadata such as GPS location.
- **Menus:** one link per line, `Title | /link` — e.g. `Sale | /collections/sale`, `Size guide | /pages/size-guide`.
- After deleting a collection or page, update menu links that pointed to it (Settings).

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
Customer accounts, GST-formatted tax invoices, courier-API integration (Shiprocket etc.), abandoned-cart emails, multiple admin users, and legal/tax advice.

## Layout
```
app.py            storefront, cart, discounts, checkout, tracking, SEO, payments (dormant)
admin.py          the whole admin panel
backup.py         database backup script
data/             starting content (first run only)
templates/        Jinja2 HTML (admin_*.html = admin)
static/           css/theme.css · js/ · img/
test_app.py       storefront checks     test_admin.py   admin checks
```
