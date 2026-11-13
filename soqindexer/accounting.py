"""Address accounting + meta key/value helpers (portable SQL)."""
import time

from .db import as_db


def now():
    return int(time.time())


def get_meta(conn, key, default=None):
    row = as_db(conn).fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(conn, key, value):
    as_db(conn).execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


def touch_address(conn, address, height, d_balance=0, d_received=0, d_sent=0, d_utxo=0):
    if not address:
        return
    db = as_db(conn)
    db.execute("INSERT INTO addresses(address,balance_sats,received_sats,sent_sats,tx_count,utxo_count,first_height,last_height) "
               "VALUES(?,0,0,0,0,0,?,?) ON CONFLICT(address) DO NOTHING", (address, height, height))
    g = db.greatest
    db.execute("UPDATE addresses SET balance_sats=balance_sats+?, received_sats=received_sats+?, sent_sats=sent_sats+?, "
               "utxo_count=%s(0,utxo_count+?), last_height=%s(last_height,?) WHERE address=?" % (g, g),
               (d_balance, d_received, d_sent, d_utxo, height, address))


def record_address_tx(conn, address, txid, height, received=0, sent=0):
    """One row per (address, txid). Returns True if a new history row was created."""
    if not address:
        return False
    db = as_db(conn)
    cur = db.execute("INSERT INTO address_txs(address,txid,height,received_sats,sent_sats) VALUES(?,?,?,0,0) "
                     "ON CONFLICT(address,txid) DO NOTHING", (address, txid, height))
    created = bool(cur.rowcount and cur.rowcount > 0)
    if created:
        db.execute("UPDATE addresses SET tx_count=tx_count+1 WHERE address=?", (address,))
    db.execute("UPDATE address_txs SET received_sats=received_sats+?, sent_sats=sent_sats+? WHERE address=? AND txid=?",
               (received, sent, address, txid))
    return created
