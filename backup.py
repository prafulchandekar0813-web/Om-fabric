"""Back up the order database.   Usage:  python backup.py

Writes a timestamped copy of store.db into ./backups and keeps the newest 30.
Run it from cron / a scheduled task (e.g. daily) and copy the folder somewhere off the server.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

here = Path(__file__).resolve().parent
db_path = Path(os.environ.get("DATABASE_PATH", here / "store.db"))
out_dir = Path(os.environ.get("BACKUP_DIR", here / "backups"))
keep = int(os.environ.get("BACKUP_KEEP", 30))

if not db_path.exists():
    raise SystemExit(f"No database at {db_path}")
out_dir.mkdir(parents=True, exist_ok=True)
target = out_dir / f"store-{datetime.now():%Y%m%d-%H%M%S}.db"
src, dst = sqlite3.connect(db_path), sqlite3.connect(target)
with dst:
    src.backup(dst)          # safe even while the site is running
src.close(); dst.close()
for old in sorted(out_dir.glob("store-*.db"))[:-keep]:
    old.unlink()
print("Backup written:", target)
