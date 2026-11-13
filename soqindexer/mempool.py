"""Mempool watcher: keeps the `mempool` table in sync with the node, fetching only new txids."""
import json
import time

from . import codec, schema
from .db import as_db


def sync(conn, rpc):
    """Diff the node mempool against the table: insert new txs, delete evicted ones. Never raises on RPC errors."""
    db = schema.ensure(conn)
    try:
        pool = rpc.call("getrawmempool", [True]) or {}
    except Exception:
        return
    if isinstance(pool, list):
        pool = {txid: {} for txid in pool}
    current = set(pool)
    known = {r["txid"] for r in db.fetchall("SELECT txid FROM mempool")}
    for txid in current - known:
        meta = pool.get(txid) or {}
        try:
            tx = rpc.call("getrawtransaction", [txid, True], timeout=15)
        except Exception:
            continue
        vin = tx.get("vin") or []
        vout = tx.get("vout") or []
        value_out = sum(codec.parse_sats(o.get("value", 0)) for o in vout)
        fee = 0
        try:
            fee = codec.parse_sats(meta.get("fee") or (meta.get("fees") or {}).get("base", 0))
        except Exception:
            pass
        db.execute("INSERT INTO mempool(txid,added_time,size,fee_sats,value_out_sats,input_count,output_count,raw_json) "
                   "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(txid) DO UPDATE SET added_time=excluded.added_time, size=excluded.size, "
                   "fee_sats=excluded.fee_sats, value_out_sats=excluded.value_out_sats, input_count=excluded.input_count, "
                   "output_count=excluded.output_count, raw_json=excluded.raw_json",
                   (txid, int(meta.get("time") or time.time()), int(meta.get("size") or meta.get("vsize") or 0), fee,
                    value_out, len(vin), len(vout), json.dumps(tx)))
    # refresh fee/size for rows we already know (older inserts may predate correct fee parsing)
    known_fees = {r["txid"]: r["fee_sats"] for r in db.fetchall("SELECT txid,fee_sats FROM mempool")}
    updates = []
    for txid, meta in pool.items():
        f = meta.get("fee")
        if f is None:
            continue
        fee_sats = int(round(float(f) * 1e8))
        if known_fees.get(txid) == fee_sats:
            continue
        updates.append((fee_sats, int(meta.get("size") or meta.get("vsize") or 0), txid))
    for fee_sats, size, txid in updates:
        db.execute("UPDATE mempool SET fee_sats=?, size=? WHERE txid=?", (fee_sats, size, txid))
    stale = known - current
    if stale:
        for txid in stale:
            db.execute("DELETE FROM mempool WHERE txid=?", (txid,))
    db.commit()


def run(db, rpc, poll=3, stop=None):
    while not (stop and stop()):
        sync(db, rpc)
        time.sleep(max(1, poll))
