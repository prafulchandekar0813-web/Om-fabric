# Thread & Co. — standalone store (Python · HTML · CSS · JS)

Your Shopify theme rebuilt as a normal website you can host anywhere that runs Python.
Same design (your original CSS), no Shopify account needed.

## Run it on your computer
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                          # open http://localhost:5000
python test_app.py                                     # run the automated checks
```

## What's included
| Feature | Notes |
|---|---|
| Storefront | Home, collections (paginated, **sortable**), product pages, **search**, bag, checkout |
| **Online payments** | Razorpay (UPI, cards, netbanking). Server-side signature verification + webhook backup |
| Cash on Delivery | Optional COD fee (`shipping.cod_fee` in `site.json`) |
| **Stock counts** | Per size/variant. Reduced when an order is placed, restored on cancel, "only N left" messages, no overselling |
| **Discount codes** | `data/discounts.json`: percent, flat ₹, free shipping; minimum order, expiry, max uses, once per customer |
| **Emails** | Receipt to the customer + alert to you; "shipped" (with tracking) and "cancelled" emails |
| **Order tracking** | `/track` — customer enters order number + email |
| **Admin** (`/admin`) | Orders, status + courier tracking, stock editor, messages, **CSV export, database backup download** |
| **SEO** | Page titles/descriptions, share tags, canonical links, product structured data, `sitemap.xml`, `robots.txt` |
| **Analytics** | Optional Google Analytics 4 or Plausible (set the ID in `site.json` → `analytics`) |
| **Policy pages** | Drafts for Shipping, Returns, Privacy, Terms (see "Before you launch") |
| Security | CSRF protection, spam honeypots, escaped output, signed cookies, password-protected admin |

## Change content (replaces the Shopify admin)
Everything is in `data/` — edit, save, refresh. No restart needed.

| What | Where |
|---|---|
| Brand, announcement bar, menus, hero, featured section, shipping/COD fee, payment methods, SEO text, analytics IDs, business name/address/GSTIN | `data/site.json` |
| Products, sizes, prices, **stock**, sale prices, collections | `data/products.json` |
| Discount codes | `data/discounts.json` |
| About / Shipping / Returns / Privacy / Terms pages (HTML allowed) | `data/pages.json` |
| Photos | put files in `static/img/…`, reference them in the JSON |
| Look & feel | `static/css/theme.css`, markup in `templates/` |

- **Counted stock:** give a variant `"stock": 10`. Leave `stock` out for "unlimited". Add `"available": false` to force "sold out". After launch, change counts in **/admin → Stock** (the number in `products.json` is only the starting value).
- **Sale price:** `price` = sale price, `compare_at_price` = original.
- **Discount codes:** the file ships with **sample codes (WELCOME10, FLAT100)** — edit or delete them before launch, or anyone who guesses them gets a discount.

## Turn on online payments (Razorpay)
1. Create and verify a Razorpay account (they handle KYC/approval — that part is yours to complete).
2. Dashboard → API Keys → generate **Test** keys first. Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` as environment variables (never in code or HTML).
3. Place a test order with Razorpay's test UPI/card details; check it shows **paid** in `/admin`.
4. Recommended: add a webhook (Dashboard → Webhooks) pointing to `https://yourshop/razorpay/webhook` with events `payment.captured` and `order.paid`, and set `RAZORPAY_WEBHOOK_SECRET` to the secret you chose.
5. When happy, swap in **Live** keys.

How it works: the order is created and stock is held for 30 minutes → customer pays in Razorpay's window → the server verifies the payment signature → order becomes "paid" and emails go out. Unpaid orders expire and release their stock automatically. Refunds are done from the Razorpay dashboard.

## Turn on emails
Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (and `NOTIFY_EMAIL` for your own alerts). Any SMTP provider works (Gmail app password, Brevo, Zoho, SES…). Without these, emails are skipped and orders still work.

## Before you launch — checklist
- [ ] Replace sample products, photos, hero image, `hello@example.com`, brand story
- [ ] Fill `business` in `site.json` (legal name, address, phone, GSTIN)
- [ ] **Review the 4 policy pages** and delete `"draft": true` from each (a red "DRAFT" banner shows until you do). They are starting points, not legal advice
- [ ] Edit/delete sample discount codes
- [ ] Set `SECRET_KEY`, `ADMIN_PASSWORD`, `SITE_URL`, `COOKIE_SECURE=1`
- [ ] Persistent disk for `DATABASE_PATH`; schedule backups (below)
- [ ] Razorpay test order → then live keys; SMTP test; place a COD test order; test on a phone

## Backups
Orders are your business records. Either download from **/admin → Download database backup**, or schedule `python backup.py` (daily cron) which keeps the newest 30 copies in `backups/` — copy that folder off the server too.

## Deploy
Start command everywhere: `gunicorn app:app`
- **Render / Railway / Fly.io:** build `pip install -r requirements.txt`, start `gunicorn app:app` (`Procfile` included). Attach a **persistent disk** and set `DATABASE_PATH` onto it, otherwise orders vanish on restart/redeploy.
- **PythonAnywhere:** upload, create a Flask web app pointing at `app.py`, set env vars in the WSGI file.
- **VPS:** gunicorn behind nginx/Caddy for https, run under systemd.
- **Docker:** `docker build -t threadandco . && docker run -p 8000:8000 -v tc-data:/data --env-file .env threadandco`

Static-only hosts (GitHub Pages, Netlify drop, S3) won't work — the cart, checkout and payments need the Python server. Run **one** server process group on **one** machine (SQLite); that comfortably handles a small store.

## Not included
Customer accounts, GST-formatted tax invoices, courier-API integration (Shiprocket etc.), abandoned-cart emails, a web form for adding products (products are edited in `products.json`), and legal/tax advice.

## Layout
```
app.py            all routes: shop, cart, discounts, checkout, Razorpay, tracking, admin, SEO
backup.py         database backup script
data/             site.json · products.json · discounts.json · pages.json
templates/        Jinja2 HTML
static/           css/theme.css · js/theme.js · js/pay.js · img/
test_app.py       automated checks
```
