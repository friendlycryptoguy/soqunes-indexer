"""Shared test DB factory. Set SOQINDEXER_TEST_DB=postgres://user:pass@host/db to run the suite on PostgreSQL."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from soqindexer import schema  # noqa: E402
from soqindexer.db import Database  # noqa: E402

PG_URL = os.environ.get("SOQINDEXER_TEST_DB", "")
_ALL = schema.CHAIN_TABLES + schema.PROTOCOL_TABLES + schema.MEMPOOL_TABLES + ("meta",)


def open_test_db(tmpdir):
    if PG_URL:
        db = schema.ensure(Database.open(PG_URL))
        for t in _ALL:
            db.execute("DELETE FROM " + t)
        db.commit()
        return db
    return schema.ensure(Database.sqlite(os.path.join(tmpdir, "test.db")))
