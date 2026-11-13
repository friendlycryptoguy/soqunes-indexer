"""Schema for the chain index, the Soqunes protocol state and the per-block undo journal.

Only chain-derived tables live here. Application tables (mints, keys, wallets) belong to
the consuming app. `ensure(db)` is idempotent and applies additive migrations.
"""
from .db import as_db

CHAIN_TABLES = ("address_txs", "addresses", "tx_outputs", "tx_inputs", "transactions", "utxos", "blocks", "block_undo")
PROTOCOL_TABLES = ("soqune_events", "soqune_utxos", "soqune_undo_utxos", "soqune_balances", "soqune_tokens", "soqune_applied")
MEMPOOL_TABLES = ("mempool",)

_TYPES = {
    "sqlite": {"int": "INTEGER", "big": "INTEGER", "real": "REAL", "text": "TEXT", "serial": "INTEGER PRIMARY KEY AUTOINCREMENT"},
    "postgres": {"int": "INTEGER", "big": "BIGINT", "real": "DOUBLE PRECISION", "text": "TEXT", "serial": "BIGSERIAL PRIMARY KEY"},
}


def ddl(dialect="sqlite"):
    t = _TYPES[dialect]
    return f"""
    CREATE TABLE IF NOT EXISTS blocks(
      height {t['big']} PRIMARY KEY, hash {t['text']} UNIQUE NOT NULL, previous_hash {t['text']} NOT NULL,
      timestamp {t['big']} NOT NULL, tx_count {t['int']} NOT NULL, size {t['big']} NOT NULL DEFAULT 0,
      difficulty {t['real']} NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS transactions(
      txid {t['text']} PRIMARY KEY, block_height {t['big']} NOT NULL, timestamp {t['big']} NOT NULL,
      fee_sats {t['big']} NOT NULL DEFAULT 0, value_out_sats {t['big']} NOT NULL DEFAULT 0,
      to_sats {t['big']} NOT NULL DEFAULT 0,
      input_count {t['int']} NOT NULL, output_count {t['int']} NOT NULL, is_coinbase {t['int']} NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tx_inputs(
      txid {t['text']} NOT NULL, vin {t['int']} NOT NULL, prev_txid {t['text']} NOT NULL, vout {t['int']} NOT NULL,
      prev_value_sats {t['big']} NOT NULL DEFAULT 0, prev_address {t['text']},
      PRIMARY KEY(txid,vin)
    );
    CREATE TABLE IF NOT EXISTS tx_outputs(
      txid {t['text']} NOT NULL, vout {t['int']} NOT NULL, value_sats {t['big']} NOT NULL,
      address {t['text']}, script_type {t['text']}, script_hex {t['text']},
      spent_by_txid {t['text']}, spent_height {t['big']},
      PRIMARY KEY(txid,vout)
    );
    CREATE TABLE IF NOT EXISTS utxos(
      txid {t['text']} NOT NULL, vout {t['int']} NOT NULL, value_sats {t['big']} NOT NULL,
      address {t['text']}, created_height {t['big']} NOT NULL, created_time {t['big']} NOT NULL,
      spent_by_txid {t['text']}, spent_height {t['big']}, spent_time {t['big']},
      status {t['int']} NOT NULL DEFAULT 0, PRIMARY KEY(txid,vout)
    );
    CREATE TABLE IF NOT EXISTS addresses(
      address {t['text']} PRIMARY KEY, balance_sats {t['big']} NOT NULL DEFAULT 0,
      received_sats {t['big']} NOT NULL DEFAULT 0, sent_sats {t['big']} NOT NULL DEFAULT 0,
      tx_count {t['int']} NOT NULL DEFAULT 0, utxo_count {t['int']} NOT NULL DEFAULT 0,
      first_height {t['big']} NOT NULL, last_height {t['big']} NOT NULL
    );
    CREATE TABLE IF NOT EXISTS address_txs(
      address {t['text']} NOT NULL, txid {t['text']} NOT NULL, height {t['big']} NOT NULL,
      received_sats {t['big']} NOT NULL DEFAULT 0, sent_sats {t['big']} NOT NULL DEFAULT 0,
      PRIMARY KEY(address,txid)
    );
    CREATE TABLE IF NOT EXISTS block_undo(
      height {t['big']} PRIMARY KEY, spent_json {t['text']} NOT NULL, addr_json {t['text']} NOT NULL
    );
    CREATE TABLE IF NOT EXISTS meta(key {t['text']} PRIMARY KEY, value {t['text']});
    CREATE TABLE IF NOT EXISTS mempool(
      txid {t['text']} PRIMARY KEY, added_time {t['big']} NOT NULL, size {t['big']} NOT NULL DEFAULT 0,
      fee_sats {t['big']} NOT NULL DEFAULT 0, value_out_sats {t['big']} NOT NULL DEFAULT 0,
      input_count {t['int']} NOT NULL DEFAULT 0, output_count {t['int']} NOT NULL DEFAULT 0,
      raw_json {t['text']}
    );
    CREATE TABLE IF NOT EXISTS soqune_tokens(
      soqune_id {t['big']} PRIMARY KEY, number {t['big']}, block {t['big']}, tx {t['int']}, name {t['text']} UNIQUE,
      divisibility {t['int']} NOT NULL DEFAULT 0, premine {t['big']} NOT NULL DEFAULT 0,
      amount {t['big']} NOT NULL DEFAULT 0, cap {t['big']} NOT NULL DEFAULT 0,
      height_start {t['big']} NOT NULL DEFAULT 0, height_end {t['big']} NOT NULL DEFAULT 0,
      offset_start {t['big']} NOT NULL DEFAULT 0, offset_end {t['big']} NOT NULL DEFAULT 0,
      symbol {t['big']} NOT NULL DEFAULT 0, spacers {t['big']} NOT NULL DEFAULT 0,
      etched_height {t['big']} NOT NULL DEFAULT 0, minted {t['big']} NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS soqune_balances(
      soqune_id {t['big']} NOT NULL, address {t['text']} NOT NULL, balance {t['big']} NOT NULL DEFAULT 0,
      PRIMARY KEY(soqune_id,address)
    );
    CREATE TABLE IF NOT EXISTS soqune_utxos(
      soqune_id {t['big']} NOT NULL, txid {t['text']} NOT NULL, vout {t['int']} NOT NULL,
      address {t['text']}, units {t['big']} NOT NULL, height {t['big']},
      PRIMARY KEY(txid,vout,soqune_id)
    );
    CREATE TABLE IF NOT EXISTS soqune_undo_utxos(
      height {t['big']} NOT NULL, txid {t['text']} NOT NULL, soqune_id {t['big']} NOT NULL,
      src_txid {t['text']} NOT NULL, src_vout {t['int']} NOT NULL, address {t['text']},
      units {t['big']} NOT NULL, src_height {t['big']}
    );
    CREATE TABLE IF NOT EXISTS soqune_events(
      id {t['serial']}, kind {t['text']} NOT NULL, soqune_id {t['big']}, address {t['text']},
      units {t['big']} NOT NULL DEFAULT 0, txid {t['text']}, height {t['big']}, created_at {t['big']} NOT NULL
    );
    CREATE TABLE IF NOT EXISTS soqune_applied(txid {t['text']} PRIMARY KEY, applied_at {t['big']} NOT NULL, height {t['big']});
    """


def index_ddl():
    return """
    CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
    CREATE INDEX IF NOT EXISTS idx_blocks_time ON blocks(timestamp);
    CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);
    CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions(timestamp);
    CREATE INDEX IF NOT EXISTS idx_tx_coinbase_time ON transactions(is_coinbase, timestamp);
    CREATE INDEX IF NOT EXISTS idx_txouts_address ON tx_outputs(address);
    CREATE INDEX IF NOT EXISTS idx_txins_prev ON tx_inputs(prev_txid,vout);
    CREATE INDEX IF NOT EXISTS idx_address_balance ON addresses(balance_sats);
    CREATE INDEX IF NOT EXISTS idx_utxos_status_value ON utxos(status,value_sats);
    CREATE INDEX IF NOT EXISTS idx_utxos_address ON utxos(address,status,value_sats);
    CREATE INDEX IF NOT EXISTS idx_utxos_created ON utxos(created_height);
    CREATE INDEX IF NOT EXISTS idx_atx_txid ON address_txs(txid);
    CREATE INDEX IF NOT EXISTS idx_atx_height ON address_txs(height);
    CREATE INDEX IF NOT EXISTS idx_sev_height ON soqune_events(height);
    CREATE INDEX IF NOT EXISTS idx_sutxo_height ON soqune_utxos(height);
    CREATE INDEX IF NOT EXISTS idx_sundo_height ON soqune_undo_utxos(height);
    CREATE INDEX IF NOT EXISTS idx_sapplied_height ON soqune_applied(height);
    """


_MIGRATIONS = (
    ("transactions", "to_sats", "BIGINT NOT NULL DEFAULT 0"),
    ("soqune_utxos", "height", "BIGINT"),
    ("soqune_applied", "height", "BIGINT"),
    ("tx_outputs", "script_hex", "TEXT"),
)

_READY = set()


def _identity(db):
    """Stable identity of the underlying database (file path / URL), never the connection object."""
    if db.dialect == "postgres":
        return ("postgres", db.url or "")
    try:
        for r in db.conn.execute("PRAGMA database_list"):
            if r[1] == "main":
                return ("sqlite", r[2] or "")
    except Exception:
        pass
    return ("sqlite", "")


def ensure(conn_or_db):
    """Create tables/indexes and apply additive migrations. Idempotent; cheap after first call per database.

    Concurrent first-time callers (indexer + mempool watcher) are serialized with a PostgreSQL
    advisory lock; `CREATE TABLE IF NOT EXISTS` is otherwise racy on PostgreSQL.
    """
    import time as _t
    db = as_db(conn_or_db)
    key = _identity(db)
    if key[1] and key in _READY:
        return db
    locked = False
    if db.dialect == "postgres":
        try:
            db.execute("SELECT pg_advisory_lock(hashtext('soqindexer_schema'))")
            locked = True
        except Exception:
            db.rollback()
    try:
        for attempt in range(3):
            try:
                db.executescript(ddl(db.dialect))      # tables (no column-dependent indexes yet)
                db.commit()
                break
            except Exception:
                db.rollback()
                if attempt == 2:
                    raise
                _t.sleep(0.2 * (attempt + 1))
        for table, col, typ in _MIGRATIONS:
            if col not in db.columns(table):
                if db.dialect == "sqlite":
                    typ = typ.replace("BIGINT", "INTEGER")
                try:
                    db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
                    db.commit()
                except Exception as exc:  # duplicate column race; PG needs a rollback to leave the aborted txn
                    db.rollback()
                    if "duplicate" not in str(exc).lower() and "exists" not in str(exc).lower():
                        raise
        db.executescript(index_ddl())                 # indexes after additive migrations
        db.commit()
    finally:
        if locked:
            try:
                db.execute("SELECT pg_advisory_unlock(hashtext('soqindexer_schema'))")
                db.commit()
            except Exception:
                db.rollback()
    _READY.add(key)
    return db
