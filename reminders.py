"""Send due abandoned-cart reminder emails.   Usage:  python reminders.py

Run it every 10–15 minutes from cron / a scheduled task (PythonAnywhere "Tasks", Render cron job, etc.).
It is safe to run often: it only sends what is due and follows the quiet-hours/unsubscribe rules.
Needs the same environment variables as the website (DATABASE_PATH, SMTP_*, SITE_URL, SECRET_KEY).
"""
import json

from app import app, _carts

with app.app_context():
    print(json.dumps(_carts.run_job()))
