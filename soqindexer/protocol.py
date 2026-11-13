"""Soqunes protocol state machine (chain is the source of truth).

Wire: OP_RETURN OP_13 (6a5d) + LEB128 tag/value integers.
Etch = commit/reveal: the reveal spends a commit output whose tx carries
`OP_RETURN COMMIT:<HASH256(message)>` and is >= COMMIT_DEPTH blocks old; else cenotaph.
Mint = tag 20 = soqune_id, exactly `amount` once per tx, within cap and height window.
Transfer = spend token UTXOs + pointer; units move to the pointed output.

All writes are height-stamped and journaled so `rollback_protocol(db, fork)` can undo
every effect above a fork height. `apply` never commits — the block owns the transaction.
"""
from . import codec
from .accounting import now
from .db import as_db

T = codec.SOQUNES_TAG
COMMIT_DEPTH = 6
hash256 = codec.hash256


def soqunes_script(outputs):
    """outputs: iterable of (vout, value_sats, address, script_type, script_hex)."""
    for item in outputs:
        sh = (item[4] if len(item) > 4 else None) or ""
        if sh.startswith("6a5d"):
            return sh
    return None


_soqunes_script = soqunes_script  # backwards compat


def parse(ints):
    """Return {etch, mint, pointer, cenotaph, fields, edicts} or None."""
    if ints is None:
        return None
    fields, edicts = {}, []
    i = 0
    cenotaph = False
    while i < len(ints):
        tag = ints[i]
        if tag == T["body"]:
            i += 1
            continue
        if i + 1 >= len(ints):
            cenotaph = True
            break
        fields[tag] = ints[i + 1]
        i += 2
    etch_keys = ("divisibility", "premine", "name", "spacers", "symbol", "amount", "cap",
                 "height_start", "height_end", "offset_start", "offset_end")
    etch = {k: fields[T[k]] for k in etch_keys if T[k] in fields}
    return {"etch": etch if etch else None, "mint": fields.get(T["mint"]), "pointer": fields.get(T["pointer"]),
            "edicts": edicts, "fields": fields, "cenotaph": cenotaph}


def _applied(db, txid):
    return bool(db.fetchone("SELECT 1 FROM soqune_applied WHERE txid=?", (txid,)))


def _mark(db, txid, height):
    db.execute("INSERT INTO soqune_applied(txid,applied_at,height) VALUES(?,?,?) "
               "ON CONFLICT(txid) DO UPDATE SET applied_at=excluded.applied_at, height=excluded.height",
               (txid, now(), height))


def _credit(db, sid, address, units):
    db.execute("INSERT INTO soqune_balances(soqune_id,address,balance) VALUES(?,?,?) "
               "ON CONFLICT(soqune_id,address) DO UPDATE SET balance=soqune_balances.balance+excluded.balance",
               (sid, address, units))


def _debit(db, sid, address, units):
    db.execute("UPDATE soqune_balances SET balance=%s(0,balance-?) WHERE soqune_id=? AND address=?" % db.greatest,
               (units, sid, address))


def _event(db, kind, sid, address, units, txid, height):
    db.execute("INSERT INTO soqune_events(kind,soqune_id,address,units,txid,height,created_at) VALUES(?,?,?,?,?,?,?)",
               (kind, sid, address, units, txid, height, now()))


def _commit_valid(db, height, inputs, message_hex):
    want = "COMMIT:" + hash256(bytes.fromhex(message_hex)).hex()
    for prev_txid, _vout in inputs:
        blk = db.fetchone("SELECT block_height FROM transactions WHERE txid=?", (prev_txid,))
        if not blk or (height - int(blk["block_height"])) < COMMIT_DEPTH:
            continue
        for o in db.fetchall("SELECT script_hex FROM tx_outputs WHERE txid=?", (prev_txid,)):
            if codec.opret_text(o["script_hex"] or "") == want:
                return True
    return False


def _output_target(outputs, pointer):
    targets = [(item[0], item[2]) for item in outputs if len(item) > 2 and item[2]]
    if not targets:
        return (None, None)
    if pointer is not None and 0 <= pointer < len(targets):
        return targets[pointer]
    return targets[0]


def _transfer(db, txid, height, parsed, inputs, outputs):
    per, src = {}, {}
    for ptxid, pvout in inputs:
        for r in db.fetchall("SELECT soqune_id,address,units,height FROM soqune_utxos WHERE txid=? AND vout=?", (ptxid, int(pvout))):
            sid, u = int(r["soqune_id"]), int(r["units"])
            if u <= 0:
                continue
            per[sid] = per.get(sid, 0) + u
            src.setdefault(sid, r["address"])
            # journal the consumed token UTXO so a rollback can restore it
            db.execute("INSERT INTO soqune_undo_utxos(height,txid,soqune_id,src_txid,src_vout,address,units,src_height) VALUES(?,?,?,?,?,?,?,?)",
                       (height, txid, sid, ptxid, int(pvout), r["address"], u, r["height"]))
            db.execute("DELETE FROM soqune_utxos WHERE txid=? AND vout=? AND soqune_id=?", (ptxid, int(pvout), sid))
            _debit(db, sid, r["address"], u)
    if not per:
        return
    vout, dest = _output_target(outputs, parsed["pointer"])
    if not dest:
        return
    for sid, u in per.items():
        db.execute("INSERT INTO soqune_utxos(soqune_id,txid,vout,address,units,height) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(txid,vout,soqune_id) DO UPDATE SET address=excluded.address, units=excluded.units, height=excluded.height",
                   (sid, txid, vout, dest, u, height))
        _credit(db, sid, dest, u)
        _event(db, "transfer", sid, dest, u, txid, height)


def _mint(db, txid, height, parsed, outputs):
    sid = int(parsed["mint"] or 0)
    tok = db.fetchone("SELECT * FROM soqune_tokens WHERE soqune_id=?", (sid,))
    if not tok:
        return
    amount = int(tok["amount"] or 0)
    if amount <= 0:
        return
    minted, cap = int(tok["minted"] or 0), int(tok["cap"] or 0)
    hstart, hend = int(tok["height_start"] or 0), int(tok["height_end"] or 0)
    if hstart and height < hstart:
        return
    if hend and height > hend:
        return
    if cap and minted + 1 > cap:
        return
    vout, dest = _output_target(outputs, parsed["pointer"])
    if not dest:
        return
    db.execute("UPDATE soqune_tokens SET minted=minted+1 WHERE soqune_id=?", (sid,))
    _credit(db, sid, dest, amount)
    db.execute("INSERT INTO soqune_utxos(soqune_id,txid,vout,address,units,height) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(txid,vout,soqune_id) DO UPDATE SET address=excluded.address, units=excluded.units, height=excluded.height",
               (sid, txid, vout, dest, amount, height))
    _event(db, "mint", sid, dest, amount, txid, height)


def _etch(db, txid, height, tx_index, message_hex, parsed, inputs, outputs):
    e = parsed["etch"]
    name = codec.base26_decode(e.get("name", 0)) if e.get("name") else ""
    if not name:
        return
    if db.fetchone("SELECT 1 FROM soqune_tokens WHERE name=?", (name,)):
        return
    if not _commit_valid(db, height, inputs, message_hex):
        return
    sid = (int(height) << 32) | int(tx_index)
    db.execute("INSERT INTO soqune_tokens(soqune_id,number,block,tx,name,divisibility,premine,amount,cap,height_start,height_end,"
               "offset_start,offset_end,symbol,spacers,etched_height,minted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
               "ON CONFLICT(soqune_id) DO NOTHING",
               (sid, sid, height, tx_index, name, int(e.get("divisibility", 0)), int(e.get("premine", 0)),
                int(e.get("amount", 0)), int(e.get("cap", 0)), int(e.get("height_start", 0)), int(e.get("height_end", 0)),
                int(e.get("offset_start", 0)), int(e.get("offset_end", 0)), int(e.get("symbol", 0)), int(e.get("spacers", 0)), height))
    premine = int(e.get("premine", 0))
    if premine > 0:
        vout, dest = _output_target(outputs, parsed["pointer"])
        if dest:
            _credit(db, sid, dest, premine)
            db.execute("INSERT INTO soqune_utxos(soqune_id,txid,vout,address,units,height) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(txid,vout,soqune_id) DO UPDATE SET address=excluded.address, units=excluded.units, height=excluded.height",
                       (sid, txid, vout, dest, premine, height))
    _event(db, "etch", sid, name, 0, txid, height)


def apply(conn, txid, height, tx_index, inputs, outputs):
    """Apply one transaction's Soqunes effects. Never commits."""
    db = as_db(conn)
    script = soqunes_script(outputs)
    if not script:
        return
    if _applied(db, txid):
        return
    try:
        ints = codec.soqunes_ints(bytes.fromhex(script))
    except (ValueError, TypeError):
        _mark(db, txid, height)           # malformed payload -> cenotaph
        return
    if ints is None:
        return
    parsed = parse(ints)
    if parsed["cenotaph"]:
        _mark(db, txid, height)
        return
    if parsed["mint"] is not None:
        _mint(db, txid, height, parsed, outputs)
    elif parsed["etch"] is not None:
        _etch(db, txid, height, tx_index, script, parsed, inputs, outputs)
    elif parsed["pointer"] is not None:
        _transfer(db, txid, height, parsed, inputs, outputs)
    _mark(db, txid, height)


def rollback_protocol(conn, fork):
    """Undo every protocol effect recorded at height > fork. Caller owns the transaction."""
    db = as_db(conn)
    # token UTXOs created above the fork: remove + debit
    for r in db.fetchall("SELECT soqune_id,txid,vout,address,units FROM soqune_utxos WHERE height>?", (fork,)):
        _debit(db, int(r["soqune_id"]), r["address"], int(r["units"]))
    db.execute("DELETE FROM soqune_utxos WHERE height>?", (fork,))
    # token UTXOs consumed by transfers above the fork: restore + credit
    for r in db.fetchall("SELECT soqune_id,src_txid,src_vout,address,units,src_height FROM soqune_undo_utxos WHERE height>?", (fork,)):
        db.execute("INSERT INTO soqune_utxos(soqune_id,txid,vout,address,units,height) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(txid,vout,soqune_id) DO UPDATE SET address=excluded.address, units=excluded.units, height=excluded.height",
                   (int(r["soqune_id"]), r["src_txid"], int(r["src_vout"]), r["address"], int(r["units"]), r["src_height"]))
        _credit(db, int(r["soqune_id"]), r["address"], int(r["units"]))
    db.execute("DELETE FROM soqune_undo_utxos WHERE height>?", (fork,))
    # mint counters
    for r in db.fetchall("SELECT soqune_id, COUNT(*) AS n FROM soqune_events WHERE kind='mint' AND height>? GROUP BY soqune_id", (fork,)):
        db.execute("UPDATE soqune_tokens SET minted=%s(0,minted-?) WHERE soqune_id=?" % db.greatest, (int(r["n"]), int(r["soqune_id"])))
    db.execute("DELETE FROM soqune_tokens WHERE etched_height>?", (fork,))
    db.execute("DELETE FROM soqune_events WHERE height>?", (fork,))
    db.execute("DELETE FROM soqune_applied WHERE height>?", (fork,))
    db.execute("DELETE FROM soqune_balances WHERE balance<=0")
