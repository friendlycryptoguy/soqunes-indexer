"""Block processing with per-block undo journal and fork-point rollback.

`process_block` indexes one block atomically and records enough to undo it:
spent outpoints (to re-mark unspent) and aggregated address deltas (to invert).
`rollback_to(fork)` undoes blocks tip→fork+1 (chain + protocol) without a full reindex.
"""
import json
import time

from . import codec, protocol, schema
from .accounting import record_address_tx, set_meta, touch_address
from .db import as_db

CHAIN_TABLES = schema.CHAIN_TABLES
PROTOCOL_TABLES = schema.PROTOCOL_TABLES


class ReorgError(Exception):
    """The block's parent does not match the indexed chain."""


def _tip(db):
    r = db.fetchone("SELECT height, hash FROM blocks ORDER BY height DESC LIMIT 1")
    return (int(r["height"]), r["hash"]) if r else (-1, "")


def process_block(conn, block, height, utxo):
    """Index one block. Returns True if indexed, False if already indexed (same hash).

    Raises ReorgError on parent/height mismatch. On failure: DB rolled back, `utxo` restored, re-raised.
    """
    db = schema.ensure(conn)
    bh = block["hash"]
    previous_hash = block.get("previousblockhash") or "0" * 64
    existing = db.fetchone("SELECT hash FROM blocks WHERE height=?", (height,))
    if existing:
        if existing["hash"] == bh:
            return False
        raise ReorgError("height %d already indexed as %s, got %s" % (height, existing["hash"][:16], bh[:16]))
    if height > 0:
        parent = db.fetchone("SELECT hash FROM blocks WHERE height=?", (height - 1,))
        if parent and parent["hash"] != previous_hash:
            raise ReorgError("block %d parent %s != indexed %s" % (height, previous_hash[:16], parent["hash"][:16]))

    snapshot = {}

    def remember(key):
        if key not in snapshot:
            snapshot[key] = utxo.get(key)

    timestamp = int(block.get("time") or 0)
    txs = block.get("tx") or []
    spent_journal = []        # [prev_txid, vout, value, address]
    addr_delta = {}           # address -> [d_balance, d_received, d_sent, d_utxo, d_txcount]

    def bump(addr, i, v):
        d = addr_delta.setdefault(addr, [0, 0, 0, 0, 0])
        d[i] += v

    try:
        for tx_index, tx in enumerate(txs):
            txid = tx["txid"]
            vin = tx.get("vin") or []
            vout = tx.get("vout") or []
            is_coinbase = bool(vin and "coinbase" in vin[0])
            value_out = total_in = 0
            events = {}
            inputs_addrs, input_refs = [], []
            for index, item in enumerate(vin):
                prev_txid = item.get("txid", "")
                vout_n = int(item.get("vout", 0))
                key = (prev_txid, vout_n)
                remember(key)
                prev = utxo.pop(key, None)
                prev_value = int(prev[0]) if prev else 0
                prev_addr = prev[1] if prev else None
                inputs_addrs.append(prev_addr)
                input_refs.append((prev_txid, vout_n))
                total_in += prev_value
                db.execute("INSERT INTO tx_inputs(txid,vin,prev_txid,vout,prev_value_sats,prev_address) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(txid,vin) DO UPDATE SET prev_txid=excluded.prev_txid, vout=excluded.vout, "
                           "prev_value_sats=excluded.prev_value_sats, prev_address=excluded.prev_address",
                           (txid, index, prev_txid, vout_n, prev_value, prev_addr))
                db.execute("UPDATE tx_outputs SET spent_by_txid=?, spent_height=? WHERE txid=? AND vout=?", (txid, height, prev_txid, vout_n))
                db.execute("UPDATE utxos SET status=1, spent_by_txid=?, spent_height=?, spent_time=? WHERE txid=? AND vout=?",
                           (txid, height, timestamp, prev_txid, vout_n))
                if prev is not None:
                    spent_journal.append([prev_txid, vout_n, prev_value, prev_addr])
                if prev_addr:
                    events.setdefault(prev_addr, [0, 0])[1] += prev_value
                    touch_address(db, prev_addr, height, d_balance=-prev_value, d_sent=prev_value, d_utxo=-1)
                    bump(prev_addr, 0, -prev_value); bump(prev_addr, 2, prev_value); bump(prev_addr, 3, -1)
            tx_outputs_list = []
            for n, item in enumerate(vout):
                value = codec.parse_sats(item.get("value", 0))
                script = item.get("scriptPubKey") or {}
                address = codec.address_of(script)
                script_type = script.get("type", "")
                value_out += value
                script_hex = script.get("hex")
                tx_outputs_list.append((n, value, address, script_type, script_hex))
                db.execute("INSERT INTO tx_outputs(txid,vout,value_sats,address,script_type,script_hex,spent_by_txid,spent_height) "
                           "VALUES(?,?,?,?,?,?,NULL,NULL) ON CONFLICT(txid,vout) DO UPDATE SET value_sats=excluded.value_sats, "
                           "address=excluded.address, script_type=excluded.script_type, script_hex=excluded.script_hex, "
                           "spent_by_txid=NULL, spent_height=NULL",
                           (txid, n, value, address, script_type, script_hex))
                db.execute("INSERT INTO utxos(txid,vout,value_sats,address,created_height,created_time,spent_by_txid,spent_height,spent_time,status) "
                           "VALUES(?,?,?,?,?,?,NULL,NULL,NULL,0) ON CONFLICT(txid,vout) DO UPDATE SET value_sats=excluded.value_sats, "
                           "address=excluded.address, created_height=excluded.created_height, created_time=excluded.created_time, "
                           "spent_by_txid=NULL, spent_height=NULL, spent_time=NULL, status=0",
                           (txid, n, value, address, height, timestamp))
                if address:
                    key = (txid, n)
                    remember(key)
                    utxo[key] = (value, address)
                    events.setdefault(address, [0, 0])[0] += value
                    touch_address(db, address, height, d_balance=value, d_received=value, d_utxo=1)
                    bump(address, 0, value); bump(address, 1, value); bump(address, 3, 1)
            fee = 0 if is_coinbase else max(0, total_in - value_out)
            in_addrs = set(a for a in inputs_addrs if a)
            to_sats = sum((v or 0) for (_n, v, addr, _st, _sh) in tx_outputs_list if (addr is None or addr not in in_addrs))
            db.execute("INSERT INTO transactions(txid,block_height,timestamp,fee_sats,value_out_sats,to_sats,input_count,output_count,is_coinbase) "
                       "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(txid) DO UPDATE SET block_height=excluded.block_height, timestamp=excluded.timestamp, "
                       "fee_sats=excluded.fee_sats, value_out_sats=excluded.value_out_sats, to_sats=excluded.to_sats, "
                       "input_count=excluded.input_count, output_count=excluded.output_count, is_coinbase=excluded.is_coinbase",
                       (txid, height, timestamp, fee, value_out, to_sats, len(vin), len(vout), 1 if is_coinbase else 0))
            protocol.apply(db, txid, height, tx_index, input_refs, tx_outputs_list)
            for address, (received, sent) in events.items():
                net = received - sent          # net effect per tx: a change output back to the same
                rx, sx = (net, 0) if net >= 0 else (0, -net)   # address is not both received and sent
                if record_address_tx(db, address, txid, height, rx, sx):
                    bump(address, 4, 1)
        db.execute("INSERT INTO blocks(height,hash,previous_hash,timestamp,tx_count,size,difficulty) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(height) DO UPDATE SET hash=excluded.hash, previous_hash=excluded.previous_hash, timestamp=excluded.timestamp, "
                   "tx_count=excluded.tx_count, size=excluded.size, difficulty=excluded.difficulty",
                   (height, bh, previous_hash, timestamp, len(txs), int(block.get("size") or 0), float(block.get("difficulty") or 0)))
        db.execute("INSERT INTO block_undo(height,spent_json,addr_json) VALUES(?,?,?) "
                   "ON CONFLICT(height) DO UPDATE SET spent_json=excluded.spent_json, addr_json=excluded.addr_json",
                   (height, json.dumps(spent_journal, separators=(",", ":")), json.dumps(addr_delta, separators=(",", ":"))))
        set_meta(db, "last_height", height)
        set_meta(db, "last_hash", bh)
        set_meta(db, "indexed_at", int(time.time()))
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        for key, prior in snapshot.items():
            if prior is None:
                utxo.pop(key, None)
            else:
                utxo[key] = prior
        raise


def rollback_block(conn, height, utxo):
    """Undo exactly one indexed block (must be the current tip). Caller owns the transaction."""
    db = as_db(conn)
    undo = db.fetchone("SELECT spent_json, addr_json FROM block_undo WHERE height=?", (height,))
    txids = [r["txid"] for r in db.fetchall("SELECT txid FROM transactions WHERE block_height=?", (height,))]
    # created outputs leave the cache
    for r in db.fetchall("SELECT txid, vout, address FROM tx_outputs WHERE txid IN (SELECT txid FROM transactions WHERE block_height=?)", (height,)):
        if r["address"]:
            utxo.pop((r["txid"], int(r["vout"])), None)
    # delete created rows
    db.execute("DELETE FROM tx_inputs WHERE txid IN (SELECT txid FROM transactions WHERE block_height=?)", (height,))
    db.execute("DELETE FROM utxos WHERE txid IN (SELECT txid FROM transactions WHERE block_height=?)", (height,))
    db.execute("DELETE FROM tx_outputs WHERE txid IN (SELECT txid FROM transactions WHERE block_height=?)", (height,))
    db.execute("DELETE FROM address_txs WHERE height=?", (height,))
    db.execute("DELETE FROM transactions WHERE block_height=?", (height,))
    db.execute("DELETE FROM blocks WHERE height=?", (height,))
    if undo:
        for prev_txid, vout, value, address in json.loads(undo["spent_json"] or "[]"):
            db.execute("UPDATE tx_outputs SET spent_by_txid=NULL, spent_height=NULL WHERE txid=? AND vout=?", (prev_txid, vout))
            db.execute("UPDATE utxos SET status=0, spent_by_txid=NULL, spent_height=NULL, spent_time=NULL WHERE txid=? AND vout=?", (prev_txid, vout))
            if address:
                utxo[(prev_txid, int(vout))] = (int(value), address)
        g = db.greatest
        for address, d in json.loads(undo["addr_json"] or "{}").items():
            db.execute("UPDATE addresses SET balance_sats=balance_sats-?, received_sats=received_sats-?, sent_sats=sent_sats-?, "
                       "utxo_count=%s(0,utxo_count-?), tx_count=%s(0,tx_count-?) WHERE address=?" % (g, g),
                       (d[0], d[1], d[2], d[3], d[4], address))
        db.execute("DELETE FROM block_undo WHERE height=?", (height,))
    db.execute("DELETE FROM addresses WHERE first_height>? AND tx_count=0 AND balance_sats=0", (height - 1,))
    return txids


def rollback_to(conn, fork, utxo):
    """Undo all blocks above `fork` (chain + protocol) in one transaction. Returns new tip height."""
    db = as_db(conn)
    tip, _ = _tip(db)
    if fork >= tip:
        return tip
    try:
        for h in range(tip, fork, -1):
            rollback_block(db, h, utxo)
        protocol.rollback_protocol(db, fork)
        row = db.fetchone("SELECT hash FROM blocks WHERE height=?", (fork,))
        set_meta(db, "last_height", fork)
        set_meta(db, "last_hash", row["hash"] if row else "")
        db.commit()
    except Exception:
        db.rollback()
        raise
    return fork


def find_fork(conn, rpc, max_depth=2000):
    """Highest indexed height whose hash the node still agrees with; -1 if none within max_depth."""
    db = as_db(conn)
    tip, _ = _tip(db)
    h = tip
    while h >= 0 and tip - h <= max_depth:
        row = db.fetchone("SELECT hash FROM blocks WHERE height=?", (h,))
        if row and row["hash"] == rpc.block_hash(h):
            return h
        h -= 1
    return -1


def reset_chain(conn):
    """Drop all chain-derived state (chain + protocol). Application tables are untouched."""
    db = schema.ensure(conn)
    for table in CHAIN_TABLES + PROTOCOL_TABLES:
        if db.table_exists(table):
            db.execute("DELETE FROM " + table)
    set_meta(db, "last_height", "-1")
    set_meta(db, "last_hash", "")
    db.commit()


def load_utxo(conn):
    db = as_db(conn)
    result = {}
    for row in db.fetchall("SELECT txid,vout,value_sats,address FROM tx_outputs WHERE spent_by_txid IS NULL AND address IS NOT NULL"):
        result[(row["txid"], int(row["vout"]))] = (int(row["value_sats"]), row["address"])
    return result
