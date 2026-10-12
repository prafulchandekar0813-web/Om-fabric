# Thread & Co. — standalone store (Python · HTML · CSS · JS)

Your Shopify theme, rebuilt as a normal website you can host anywhere that runs Python.
Same design (the CSS is your original `theme.css`), but no Shopify account needed.

## Run it on your computer
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                          # open http://localhost:5000
```
Run the checks any time with `python test_app.py`.

## Change content (replaces the Shopify admin)
Everything is in `data/` — edit, save, refresh. No restart needed.

| What | Where |
|---|---|
| Brand name, announcement bar, menus, footer, hero text, featured section, shipping rates, contact email | `data/site.json` |
| Products, variants (sizes), prices, sale prices (`compare_at_price`), collections | `data/products.json` |
| About / Shipping / Returns pages (HTML allowed) | `data/pages.json` |
| Product & hero photos | put files in `static/img/…` and reference them in the JSON |
| Look and feel | `static/css/theme.css` |
| Layout / markup | `templates/*.html` |

- **Sale price:** set `price` to the sale price and `compare_at_price` to the original.
- **Sold out:** set a variant's `"available": false`.
- **New page:** add an entry to `pages.json`, then link `/pages/<key>` in `site.json` → `nav`.
- **Shipping:** `flat_rate` and `free_over` in `site.json` → `shipping`.
- Sample products and placeholder images are included — replace them with yours.

## What works out of the box
Home, collections (paginated), product pages with size/option picker, bag, checkout with
India-style validation (mobile, PIN code), **Cash on Delivery** orders, order confirmation,
contact form, spam honeypot, CSRF protection, and an owner page at `/admin`.

**Orders and messages** are saved in SQLite (`store.db`). To see them, set `ADMIN_PASSWORD`
and open `/admin` (username `admin`) — you can also mark orders confirmed/shipped/delivered.
Set `SMTP_*` + `NOTIFY_EMAIL` (see `.env.example`) to be emailed on every new order/message.

## What is NOT included — read this before going live
- **Online payments (UPI/cards).** Shopify provided this; here only COD is built in. To take
  online payments, integrate a gateway such as Razorpay: create the order on the server,
  verify the payment signature on the server, and keep API secrets in environment
  variables only (never in HTML/JS). Hook it into `checkout()` in `app.py`.
- **Stock counts.** Items are simply available / sold out (`available` flag), not counted.
- **Customer accounts, discount codes, tax invoices, shipping-carrier integration.**
- **Legal/tax compliance** (policies, GST invoicing, etc.) is up to you.
- **Backups.** Back up `store.db` — it is your order history.

## Deploy
Always set `SECRET_KEY` (random, long), and `COOKIE_SECURE=1` once you have https.
The start command everywhere is: `gunicorn app:app`

- **Render / Railway / Fly.io / Heroku-style hosts:** connect the repo, build `pip install -r requirements.txt`,
  start `gunicorn app:app` (a `Procfile` is included). Attach a **persistent disk/volume** and set
  `DATABASE_PATH` to a path on it, otherwise orders vanish when the app restarts.
- **PythonAnywhere:** upload the folder, create a Flask web app pointing at `app.py`, set env vars in the WSGI file.
- **VPS (Ubuntu etc.):** `gunicorn` behind nginx/Caddy for https, run as a systemd service.
- **Docker:** `docker build -t threadandco . && docker run -p 8000:8000 -v tc-data:/data -e SECRET_KEY=... threadandco`

Plain static hosts (GitHub Pages, Netlify drop, S3) will **not** work: the cart, checkout and
contact form need the Python server.

## Project layout
```
app.py            routes, cart, checkout, contact, admin
data/             site.json · products.json · pages.json  (your content)
templates/        Jinja2 HTML (ported from the Liquid sections)
static/           css/theme.css · js/theme.js · img/
test_app.py       end-to-end smoke tests
```
