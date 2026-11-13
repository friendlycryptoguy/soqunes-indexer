"""Thin database layer: SQLite (default, embedded) or PostgreSQL (production) behind one API.

Design: the indexer is the *single writer*. SQL in this package is written in the portable
subset supported by both engines (UPSERT via `ON CONFLICT`, `COALESCE`, named columns) and
`?` placeholders are translated to `%s` for PostgreSQL. Engine-specific bits (DDL types,
`MAX`/`GREATEST`, pragmas) live behind `Database` properties.

Any function in this package accepts either a `Database` or a raw `sqlite3.Connection`
(wrapped on the fly) so existing callers and tests keep working.
"""
import os
import sqlite3


class Database:
    def __init__(self, conn, dialect="sqlite", url=None):
        self.conn = conn
        self.dialect = dialect
        self.url = url

    # -- construction ------------------------------------------------------
    @classmethod
    def open(cls, url_or_path, busy_timeout_ms=60000):
        """`postgres://…`/`postgresql://…` -> PostgreSQL (psycopg 3); anything else -> SQLite file path."""
        if str(url_or_path).startswith(("postgres://", "postgresql://")):
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("PostgreSQL backend needs `pip install psycopg[binary]`") from exc
            conn = psycopg.connect(url_or_path, row_factory=dict_row, autocommit=False)
            return cls(conn, "postgres", url_or_path)
        return cls.sqlite(url_or_path, busy_timeout_ms)

    @classmethod
    def sqlite(cls, path, busy_timeout_ms=60000):
        conn = sqlite3.connect(path, timeout=max(1, busy_timeout_ms // 1000))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return cls(conn, "sqlite", path)

    # -- query API ---------------------------------------------------------
    def _sql(self, sql):
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql, params=()):
        return self.conn.execute(self._sql(sql), tuple(params))

    def executemany(self, sql, seq):
        cur = self.conn.cursor()
        cur.executemany(self._sql(sql), [tuple(p) for p in seq])
        return cur

    def executescript(self, script):
        if self.dialect == "sqlite":
            self.conn.executescript(script)
        else:
            for stmt in [s.strip() for s in script.split(";") if s.strip()]:
                self.conn.execute(stmt)

    def fetchone(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    # sqlite3.Connection-compatible context manager: commit on success, rollback on error
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit()
        return False

    def close(self):
        self.conn.close()

    # -- dialect helpers ---------------------------------------------------
    @property
    def greatest(self):
        """Scalar max function name: SQLite `MAX(a,b)`, PostgreSQL `GREATEST(a,b)`."""
        return "GREATEST" if self.dialect == "postgres" else "MAX"

    def columns(self, table):
        if self.dialect == "postgres":
            rows = self.fetchall("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))
            return {r["column_name"] for r in rows}
        return {r["name"] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}

    def table_exists(self, table):
        if self.dialect == "postgres":
            return bool(self.fetchone("SELECT 1 FROM information_schema.tables WHERE table_name=?", (table,)))
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def checkpoint(self):
        if self.dialect == "sqlite":
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


def as_db(conn_or_db):
    """Accept a Database or a raw sqlite3.Connection."""
    if isinstance(conn_or_db, Database):
        return conn_or_db
    if isinstance(conn_or_db, sqlite3.Connection):
        return Database(conn_or_db, "sqlite")
    # duck-typed proxies (tests inject failing connections)
    return Database(conn_or_db, getattr(conn_or_db, "dialect", "sqlite"))


def default_url():
    return os.environ.get("SOQINDEXER_DB", os.environ.get("SOQUNES_DB", "soqindexer.db"))
