"""Indexer main loop: follow the node tip, detect reorgs, roll back to the fork point, re-index.

Graceful shutdown on SIGINT/SIGTERM (finishes the current block, then exits).
"""
import logging
import signal
import sqlite3
import time

from . import chain, schema
from .accounting import get_meta, set_meta
from .db import Database, as_db
from .rpc import SoqRpc

log = logging.getLogger("soqindexer")


class Stop:
    def __init__(self):
        self.flag = False

    def install(self):
        def _h(signum, _frame):
            log.info("signal %s received; stopping after current block", signum)
            self.flag = True
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, _h)
            except (ValueError, OSError):  # not main thread
                pass
        return self

    def __call__(self):
        return self.flag


def backfill_difficulty(conn, rpc, limit=2500):
    db = as_db(conn)
    rows = db.fetchall("SELECT height FROM blocks WHERE height>=((SELECT MAX(height) FROM blocks)-?) AND (difficulty IS NULL OR difficulty<=0) ORDER BY height DESC", (limit,))
    if not rows:
        return 0
    updated = 0
    for row in rows:
        try:
            block = rpc.block(row["height"])
            difficulty = float(block.get("difficulty") or 0)
            if difficulty > 0:
                db.execute("UPDATE blocks SET difficulty=? WHERE height=?", (difficulty, row["height"]))
                updated += 1
                if updated % 250 == 0:
                    db.commit()
        except Exception as exc:
            log.warning("difficulty backfill %d failed: %s", row["height"], exc)
            break
    db.commit()
    return updated


def handle_reorg(db, rpc, utxo, max_rollback=2000):
    """Roll back to the fork point if it is within `max_rollback` blocks, else full reset. Returns new tip."""
    fork = chain.find_fork(db, rpc, max_depth=max_rollback)
    tip = int(get_meta(db, "last_height", "-1"))
    if fork < 0 or tip - fork > max_rollback:
        log.warning("reorg deeper than %d blocks (fork=%d); full reindex", max_rollback, fork)
        chain.reset_chain(db)
        utxo.clear()
        return -1
    log.warning("reorg: rolling back %d block(s) to fork %d", tip - fork, fork)
    chain.rollback_to(db, fork, utxo)
    return fork


def run(db, rpc, poll=2, batch=200, stop=None, max_rollback=2000):
    db = schema.ensure(db)
    stop = stop or (lambda: False)
    last = int(get_meta(db, "last_height", "-1"))
    utxo = chain.load_utxo(db)
    log.info("starting at %d with %d utxos", last, len(utxo))
    try:
        backfill_difficulty(db, rpc)
    except Exception as exc:
        log.warning("difficulty backfill skipped: %s", exc)
    while not stop():
        try:
            info = rpc.info()
            target = int(info.get("blocks", -1))
            headers = int(info.get("headers", target))
        except Exception as exc:
            log.warning("node unavailable: %s", exc)
            time.sleep(3)
            continue
        node_synced = target >= 0 and target >= headers
        # node behind our tip: wait while it catches up on an indexed ancestor, else reorg
        if target < last:
            try:
                row = db.fetchone("SELECT hash FROM blocks WHERE height=?", (target,))
                same = bool(row and row["hash"] == rpc.block_hash(target))
            except Exception:
                same = False
            if headers > target or same:
                set_meta(db, "chain_height", target); db.commit()
                time.sleep(poll)
                continue
            last = handle_reorg(db, rpc, utxo, max_rollback)
            continue
        # at tip: verify our tip hash still matches
        if last >= target:
            if last >= 0:
                try:
                    if rpc.block_hash(last) != get_meta(db, "last_hash"):
                        last = handle_reorg(db, rpc, utxo, max_rollback)
                        continue
                except Exception:
                    pass
            try:
                set_meta(db, "chain_height", target)
                set_meta(db, "node_synced", "1" if node_synced else "0")
                db.commit()
            except Exception as exc:
                db.rollback(); log.warning("meta commit: %s", exc)
            time.sleep(poll)
            continue
        # advance
        for height in range(last + 1, min(target, last + batch) + 1):
            if stop():
                break
            try:
                block = rpc.block(height)
            except Exception as exc:
                log.warning("block %d fetch failed: %s", height, exc)
                break
            try:
                chain.process_block(db, block, height, utxo)
            except chain.ReorgError as exc:
                log.warning("reorg detected at %d (%s)", height, exc)
                last = handle_reorg(db, rpc, utxo, max_rollback)
                break
            except Exception as exc:          # any DB/backend error: state was rolled back by process_block
                log.warning("block %d db error (%s); retry", height, exc)
                try:
                    db.rollback()
                except Exception:
                    pass
                break
            last = height
            if height % 100 == 0 or height == target:
                log.info("height %d %s", height, block.get("hash", "")[:16])
            if height % 250 == 0:
                db.checkpoint()
        try:
            set_meta(db, "chain_height", target); db.commit()
        except Exception as exc:
            db.rollback(); log.warning("meta commit: %s", exc)
        time.sleep(poll)
    log.info("stopped at %d", last)


def main(argv=None):
    """Compatibility entry point used by the app's indexer.py shim."""
    from .cli import main as cli_main
    return cli_main(["run"] + (argv or []))
