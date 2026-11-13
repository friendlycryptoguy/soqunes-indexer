"""Standalone tests for the `soqindexer` package (no app `core` module required).

Covers: fork-point rollback (undo journal), find_fork/handle_reorg, re-index after rollback,
mempool diffing, RPC retry policy, Database abstraction, schema migrations, codec fallbacks.
"""
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import open_test_db  # noqa: E402

from soqindexer import chain, codec, mempool, protocol, runner, schema  # noqa: E402
from soqindexer.db import Database, as_db  # noqa: E402
from soqindexer.rpc import RpcError, SoqRpc  # noqa: E402
from soqindexer.accounting import get_meta  # noqa: E402

A, B, C, D = ("sq1a" + "0" * 36, "sq1b" + "0" * 36, "sq1c" + "0" * 36, "sq1d" + "0" * 36)


def out(value, address=None, hex_=None, singular=False):
    spk = {"type": "witness_v1_scripthash"}
    if address:
        spk["address" if singular else "addresses"] = address if singular else [address]
    if hex_:
        spk["hex"] = hex_
    return {"value": value, "scriptPubKey": spk}


def opret(hex_):
    return {"value": 0.0, "scriptPubKey": {"type": "nulldata", "hex": hex_}}


def tx(txid, outputs, inputs=None, coinbase=False):
    vin = [{"coinbase": "00"}] if coinbase else [{"txid": p, "vout": v} for p, v in (inputs or [])]
    return {"txid": txid, "vin": vin, "vout": outputs}


def bhash(h, fork_tag=""):
    return ("%s%063d" % (fork_tag or "b", h))[:64]


def block(height, txs, tag="", prev_tag=None):
    prev_tag = tag if prev_tag is None else prev_tag
    return {"height": height, "hash": bhash(height, tag), "previousblockhash": bhash(height - 1, prev_tag) if height > 0 else "0" * 64,
            "time": 1_700_000_000 + height * 60, "size": 400, "difficulty": 1.0, "tx": txs}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_test_db(self.tmp.name)
        self.utxo = {}

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def apply(self, height, txs, **kw):
        return chain.process_block(self.db, block(height, txs, **kw), height, self.utxo)

    def one(self, sql, params=()):
        return self.db.fetchone(sql, params)

    def snap(self):
        def rows(sql):
            return [tuple(dict(r).values()) for r in self.db.fetchall(sql)]
        return {
            "utxos": rows("SELECT txid,vout,status,spent_by_txid FROM utxos ORDER BY txid,vout"),
            "outs": rows("SELECT txid,vout,spent_by_txid FROM tx_outputs ORDER BY txid,vout"),
            "addr": rows("SELECT address,balance_sats,received_sats,sent_sats,tx_count,utxo_count FROM addresses ORDER BY address"),
            "atx": rows("SELECT address,txid,received_sats,sent_sats FROM address_txs ORDER BY address,txid"),
            "bal": rows("SELECT soqune_id,address,balance FROM soqune_balances WHERE balance>0 ORDER BY soqune_id,address"),
            "sutxo": rows("SELECT soqune_id,txid,vout,address,units FROM soqune_utxos ORDER BY txid,vout"),
            "tok": rows("SELECT soqune_id,name,minted FROM soqune_tokens ORDER BY soqune_id"),
            "blocks": rows("SELECT height,hash FROM blocks ORDER BY height"),
            "cache": sorted(self.utxo.items()),
            "last": get_meta(self.db, "last_height"),
        }

    def etch(self, name="TKN", amount=10, cap=100):
        rs = codec.soqunes_encode_etch(name, 0, amount, cap)
        commit = codec.encode_op_return("COMMIT:" + codec.hash256(bytes.fromhex(rs)).hex())
        self.apply(1, [tx("commit", [out(0.1, A), opret(commit)], coinbase=True)])
        for h in range(2, 7):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(7, [tx("reveal", [out(0.05, B), opret(rs)], inputs=[("commit", 0)])])
        return int(self.one("SELECT soqune_id FROM soqune_tokens WHERE name=?", (name,))["soqune_id"])


class RollbackTests(Base):
    def test_rollback_restores_chain_protocol_and_cache_exactly(self):
        sid = self.etch(amount=10, cap=100)
        self.apply(8, [tx("cb8", [out(1.0, A)], coinbase=True), tx("m1", [out(0.01, C), opret(codec.soqunes_encode_mint(sid))], inputs=[("cb2", 0)])])
        before = self.snap()
        self.apply(9, [tx("x1", [out(0.005, D), opret(codec.soqunes_encode_transfer(0))], inputs=[("m1", 0)])])
        self.apply(10, [tx("m2", [out(0.01, D), opret(codec.soqunes_encode_mint(sid))], inputs=[("cb3", 0)]),
                        tx("s1", [out(0.9, B)], inputs=[("cb4", 0)])])
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 2)
        self.assertEqual(self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D))["balance"], 20)
        tip = chain.rollback_to(self.db, 8, self.utxo)
        self.assertEqual(tip, 8)
        self.assertEqual(self.snap(), before)
        self.assertIsNone(self.one("SELECT 1 FROM block_undo WHERE height>8"))
        self.assertIsNone(self.one("SELECT 1 FROM soqune_undo_utxos"))

    def test_rollback_then_reindex_alternative_chain(self):
        self.apply(1, [tx("cb1", [out(5.0, A)], coinbase=True)])
        self.apply(2, [tx("cb2", [out(5.0, A)], coinbase=True), tx("s", [out(4.9, B)], inputs=[("cb1", 0)])])
        chain.rollback_to(self.db, 1, self.utxo)
        self.assertIn(("cb1", 0), self.utxo)
        with self.assertRaises(chain.ReorgError):
            self.apply(2, [tx("cb2x", [out(5.0, C)], coinbase=True)], tag="c", prev_tag="x")   # wrong parent
        ok = self.apply(2, [tx("cb2x", [out(5.0, C)], coinbase=True)], tag="c", prev_tag="b")   # parent = our block 1
        self.assertTrue(ok)
        self.assertEqual(self.one("SELECT hash FROM blocks WHERE height=2")["hash"], bhash(2, "c"))
        self.assertEqual(self.one("SELECT balance_sats FROM addresses WHERE address=?", (A,))["balance_sats"], 5_00000000)
        self.assertIsNone(self.one("SELECT 1 FROM addresses WHERE address=?", (B,)))

    def test_rollback_noop_when_fork_at_or_above_tip(self):
        self.apply(1, [tx("cb1", [out(1.0, A)], coinbase=True)])
        self.assertEqual(chain.rollback_to(self.db, 1, self.utxo), 1)
        self.assertEqual(chain.rollback_to(self.db, 5, self.utxo), 1)

    def test_find_fork_and_handle_reorg(self):
        for h in range(1, 6):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])

        class Rpc:
            def block_hash(self, h):
                return bhash(h) if h <= 3 else bhash(h, "z")   # node disagrees above 3
        self.assertEqual(chain.find_fork(self.db, Rpc()), 3)
        tip = runner.handle_reorg(self.db, Rpc(), self.utxo, max_rollback=100)
        self.assertEqual(tip, 3)
        self.assertEqual(self.one("SELECT MAX(height) h FROM blocks")["h"], 3)

    def test_deep_reorg_falls_back_to_full_reset(self):
        for h in range(1, 6):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])

        class Rpc:
            def block_hash(self, h):
                return "ff" * 32
        tip = runner.handle_reorg(self.db, Rpc(), self.utxo, max_rollback=2)
        self.assertEqual(tip, -1)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM blocks")["n"], 0)
        self.assertEqual(self.utxo, {})

    def test_rollback_removes_addresses_created_above_fork(self):
        self.apply(1, [tx("cb1", [out(1.0, A)], coinbase=True)])
        self.apply(2, [tx("cb2", [out(1.0, B)], coinbase=True)])
        chain.rollback_to(self.db, 1, self.utxo)
        self.assertIsNone(self.one("SELECT 1 FROM addresses WHERE address=?", (B,)))
        self.assertIsNotNone(self.one("SELECT 1 FROM addresses WHERE address=?", (A,)))


class MempoolDiffTests(Base):
    def test_only_new_txids_are_fetched(self):
        calls = {"raw": 0}
        pool = {"t1": {"time": 1, "size": 100, "fees": {"base": 0.001}}, "t2": {}}

        class Rpc:
            def call(self, method, params=None, timeout=None):
                if method == "getrawmempool":
                    return pool
                calls["raw"] += 1
                return {"txid": params[0], "vin": [], "vout": [out(1.0, A)]}
        mempool.sync(self.db, Rpc()); self.assertEqual(calls["raw"], 2)
        mempool.sync(self.db, Rpc()); self.assertEqual(calls["raw"], 2)          # nothing new
        pool["t3"] = {}
        mempool.sync(self.db, Rpc()); self.assertEqual(calls["raw"], 3)          # one new
        del pool["t1"]
        mempool.sync(self.db, Rpc())
        self.assertEqual(sorted(r["txid"] for r in self.db.fetchall("SELECT txid FROM mempool")), ["t2", "t3"])


class RpcRetryTests(unittest.TestCase):
    def test_transport_errors_are_retried_then_succeed(self):
        rpc = SoqRpc("http://127.0.0.1:1/", "u", "p", retries=2, backoff=0)
        attempts = {"n": 0}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"result": 42, "error": null}'

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.URLError("down")
            return Resp()
        import soqindexer.rpc as rpcmod
        orig = rpcmod.urllib.request.urlopen
        rpcmod.urllib.request.urlopen = fake_urlopen
        try:
            self.assertEqual(rpc.call("getblockcount"), 42)
            self.assertEqual(attempts["n"], 3)
        finally:
            rpcmod.urllib.request.urlopen = orig

    def test_rpc_level_error_is_not_retried(self):
        rpc = SoqRpc("http://127.0.0.1:1/", "u", "p", retries=3, backoff=0)
        attempts = {"n": 0}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"result": null, "error": {"message": "bad method"}}'

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            return Resp()
        import soqindexer.rpc as rpcmod
        orig = rpcmod.urllib.request.urlopen
        rpcmod.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(RpcError):
                rpc.call("nope")
            self.assertEqual(attempts["n"], 1)
        finally:
            rpcmod.urllib.request.urlopen = orig


class DatabaseTests(unittest.TestCase):
    def test_placeholder_translation_only_for_postgres(self):
        class Dummy:
            def execute(self, sql, params): return (sql, params)
        self.assertEqual(Database(Dummy(), "postgres").execute("SELECT ? , ?", (1, 2))[0], "SELECT %s , %s")
        self.assertEqual(Database(Dummy(), "sqlite").execute("SELECT ? , ?", (1, 2))[0], "SELECT ? , ?")
        self.assertEqual(Database(Dummy(), "postgres").greatest, "GREATEST")
        self.assertEqual(Database(Dummy(), "sqlite").greatest, "MAX")

    def test_as_db_wraps_raw_sqlite_and_passes_through(self):
        import sqlite3
        raw = sqlite3.connect(":memory:")
        db = as_db(raw)
        self.assertIs(as_db(db), db)
        self.assertEqual(db.dialect, "sqlite")

    def test_schema_ensure_is_idempotent_and_migrates_old_tables(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "old.db")
        raw = sqlite3.connect(path); raw.row_factory = sqlite3.Row
        raw.execute("CREATE TABLE soqune_applied(txid TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")   # legacy: no height
        raw.commit()
        db = schema.ensure(raw)
        self.assertIn("height", db.columns("soqune_applied"))
        self.assertTrue(db.table_exists("block_undo"))
        schema.ensure(raw)   # second call: no error
        raw.close(); tmp.cleanup()


class CodecFallbackTests(unittest.TestCase):
    def test_script_to_address_for_witness_v1(self):
        addr = codec.script_to_address("5120" + "11" * 32)
        self.assertTrue(addr.startswith("sq1p"))
        self.assertEqual(len(addr), 62)
        self.assertIsNone(codec.script_to_address("6a04deadbeef"))
        self.assertIsNone(codec.script_to_address("zz"))

    def test_address_of_precedence(self):
        self.assertEqual(codec.address_of({"addresses": ["x"], "address": "y", "hex": "5120" + "00" * 32}), "x")
        self.assertEqual(codec.address_of({"address": "y", "hex": "5120" + "00" * 32}), "y")
        self.assertTrue(codec.address_of({"hex": "5120" + "00" * 32}).startswith("sq1p"))
        self.assertIsNone(codec.address_of({"hex": "6a00"}))
        self.assertIsNone(codec.address_of({}))

    def test_decoded_address_is_indexed_when_node_omits_addresses(self):
        tmp = tempfile.TemporaryDirectory()
        db = open_test_db(tmp.name)
        utxo = {}
        chain.process_block(db, block(1, [tx("cb1", [out(1.0, None, hex_="5120" + "22" * 32)], coinbase=True)]), 1, utxo)
        row = db.fetchone("SELECT address FROM tx_outputs WHERE txid='cb1'")
        self.assertTrue(row["address"].startswith("sq1p"))
        self.assertIn(("cb1", 0), utxo)
        db.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
