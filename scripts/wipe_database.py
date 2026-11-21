"""Erase ALL app data in DATABASE_URL and recreate empty tables (e.g. after Sandbox testing,
before connecting real accounts).

Safety: refuses to run if any Production (real bank) connection is stored, because
deleting those access tokens would waste Plaid Trial connection slots.

    python scripts/wipe_database.py --i-understand-this-deletes-everything
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import normalize_db_url  # noqa: E402

if "--i-understand-this-deletes-everything" not in sys.argv:
    raise SystemExit(__doc__)
url = normalize_db_url(get_settings().database_url)
eng = create_engine(url)
with eng.begin() as c:
    try:
        n = c.execute(text("SELECT count(*) FROM plaid_items WHERE plaid_env = 'production'")).scalar()
    except Exception:
        n = 0
    if n:
        raise SystemExit(f"Refusing: {n} real (Production) connection(s) are stored here.")
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
eng.dispose()
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

root = Path(__file__).resolve().parents[1]
cfg = Config(str(root / "alembic.ini"))
cfg.set_main_option("script_location", str(root / "alembic"))
command.upgrade(cfg, "head")
print("Database wiped and recreated.")
