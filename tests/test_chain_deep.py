"""Deep tests for the Soqucoin chain indexer + Soqunes protocol state machine.

Covers the consensus-critical paths that a reusable, open-source indexer must get right:

  * chain linkage / reorg detection (prevhash, same-height-different-hash, genesis)
  * idempotent re-indexing (no double counting)
  * atomic block processing: a failure mid-block rolls back chain rows AND protocol state,
    and restores the in-memory UTXO cache so a retry is consistent
  * malformed messages are cenotaphs, never crashes
  * reset_chain clears protocol state together with chain state
  * address extraction fallbacks (`addresses[]` / singular `address`)
  * Soqunes protocol end-to-end through the block pipeline: commit/reveal etch,
    commit depth, mint (pointer, cap, height window), transfer, idempotent apply
  * to_sats heuristic, large blocks, mempool watcher diffing

Run:  python3 -m unittest discover -s tests -v
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import open_test_db  # noqa: E402
from soqindexer import chain, codec, mempool, protocol, runner  # noqa: E402
from soqindexer.accounting import get_meta  # noqa: E402
import sqlite3



A = "sq1testalice000000000000000000000000000"
B = "sq1testbob00000000000000000000000000000"
C = "sq1testcarol000000000000000000000000000"
D = "sq1testdave0000000000000000000000000000"


def out(value, address=None, hex_=None, type_="witness_v1_scripthash", singular=False):
    spk = {"type": type_}
    if address:
        if singular:
            spk["address"] = address
        else:
            spk["addresses"] = [address]
    if hex_:
        spk["hex"] = hex_
    return {"value": value, "scriptPubKey": spk}


def opret(hex_):
    return {"value": 0.0, "scriptPubKey": {"type": "nulldata", "hex": hex_}}


def tx(txid, outputs, inputs=None, coinbase=False):
    vin = [{"coinbase": "03deadbeef"}] if coinbase else [{"txid": p, "vout": v} for p, v in (inputs or [])]
    return {"txid": txid, "vin": vin, "vout": outputs}


def bhash(h):
    return "b%063d" % h


def block(height, txs, previous=None, when=None, difficulty=1.0, hash_=None):
    return {
        "height": height,
        "hash": hash_ or bhash(height),
        "previousblockhash": previous if previous is not None else (bhash(height - 1) if height > 0 else "0" * 64),
        "time": when if when is not None else 1_700_000_000 + height * 60,
        "size": 500 + height,
        "difficulty": difficulty,
        "tx": txs,
    }


def h256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class FailingConn:
    """Connection proxy that raises on a specific statement (to test atomic rollback)."""

    def __init__(self, conn, needle, when_param0):
        self._c = conn
        self.needle = needle
        self.when = when_param0

    def execute(self, sql, params=()):
        if self.needle in sql and params and params[0] == self.when:
            raise sqlite3.OperationalError("injected failure")
        return self._c.execute(sql, params)

    def commit(self):
        return self._c.commit()

    def rollback(self):
        return self._c.rollback()

    def __getattr__(self, name):
        return getattr(self._c, name)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_test_db(self.tmp.name)
        self.utxo = {}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def apply(self, height, txs, **kw):
        b = block(height, txs, **kw)
        chain.process_block(self.conn, b, height, self.utxo)
        return b

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def count(self, table, where="1=1", params=()):
        return self.one("SELECT COUNT(*) n FROM %s WHERE %s" % (table, where), params)["n"]


# ----------------------------------------------------------------------------
# Chain linkage, reorg detection, idempotency
# ----------------------------------------------------------------------------
class ChainLinkageTests(Base):
    def test_genesis_ignores_previous_hash(self):
        self.apply(0, [tx("cb0", [out(50.0, A)], coinbase=True)], previous="ffff" * 16)
        self.assertEqual(self.count("blocks"), 1)

    def test_matching_parent_is_indexed(self):
        self.apply(1, [tx("cb1", [out(50.0, A)], coinbase=True)])
        self.apply(2, [tx("cb2", [out(50.0, A)], coinbase=True)], previous=bhash(1))
        self.assertEqual(self.count("blocks"), 2)

    def test_mismatching_parent_raises_reorg_and_changes_nothing(self):
        self.apply(1, [tx("cb1", [out(50.0, A)], coinbase=True)])
        before_tx = self.count("transactions")
        with self.assertRaises(chain.ReorgError):
            self.apply(2, [tx("cb2", [out(50.0, A)], coinbase=True)], previous="deadbeef" * 8)
        self.assertEqual(self.count("blocks"), 1)
        self.assertEqual(self.count("transactions"), before_tx)
        self.assertNotIn(("cb2", 0), self.utxo)

    def test_same_height_different_hash_raises_reorg(self):
        self.apply(1, [tx("cb1", [out(50.0, A)], coinbase=True)])
        with self.assertRaises(chain.ReorgError):
            chain.process_block(self.conn, block(1, [tx("cb1b", [out(50.0, B)], coinbase=True)], hash_="c" * 64), 1, self.utxo)
        self.assertEqual(self.one("SELECT hash FROM blocks WHERE height=1")["hash"], bhash(1))
        self.assertIsNone(self.one("SELECT 1 FROM addresses WHERE address=?", (B,)))

    def test_reindexing_same_block_is_a_noop(self):
        b = self.apply(1, [tx("cb1", [out(50.0, A)], coinbase=True)])
        again = chain.process_block(self.conn, b, 1, self.utxo)
        self.assertFalse(again)
        row = self.one("SELECT balance_sats, received_sats, tx_count FROM addresses WHERE address=?", (A,))
        self.assertEqual((row["balance_sats"], row["received_sats"], row["tx_count"]), (50_00000000, 50_00000000, 1))

    def test_missing_parent_row_is_tolerated(self):
        # starting mid-chain (no block 4 indexed) must still index block 5
        self.apply(5, [tx("cb5", [out(1.0, A)], coinbase=True)], previous="a" * 64)
        self.assertEqual(self.count("blocks"), 1)


# ----------------------------------------------------------------------------
# Atomic failure handling
# ----------------------------------------------------------------------------
class AtomicityTests(Base):
    def test_failure_rolls_back_rows_and_restores_utxo_cache(self):
        self.apply(1, [tx("cb1", [out(50.0, A)], coinbase=True)])
        self.assertIn(("cb1", 0), self.utxo)
        failing = FailingConn(self.conn, "INSERT INTO transactions", "boom")
        b = block(2, [
            tx("spend1", [out(49.0, B)], inputs=[("cb1", 0)]),   # succeeds (mutates cache)
            tx("boom", [out(1.0, C)], coinbase=True),            # injected failure here
        ], previous=bhash(1))
        with self.assertRaises(sqlite3.OperationalError):
            chain.process_block(failing, b, 2, self.utxo)
        # DB: nothing from block 2
        self.assertEqual(self.count("blocks"), 1)
        self.assertIsNone(self.one("SELECT 1 FROM transactions WHERE txid='spend1'"))
        self.assertEqual(self.one("SELECT status FROM utxos WHERE txid='cb1'")["status"], 0)
        self.assertEqual(self.one("SELECT balance_sats FROM addresses WHERE address=?", (A,))["balance_sats"], 50_00000000)
        self.assertIsNone(self.one("SELECT 1 FROM addresses WHERE address=?", (B,)))
        # cache: popped input restored, created output removed
        self.assertIn(("cb1", 0), self.utxo)
        self.assertNotIn(("spend1", 0), self.utxo)
        # retry with a healthy connection works and is consistent
        chain.process_block(self.conn, block(2, [tx("spend1", [out(49.0, B)], inputs=[("cb1", 0)])], previous=bhash(1)), 2, self.utxo)
        self.assertEqual(self.one("SELECT balance_sats FROM addresses WHERE address=?", (A,))["balance_sats"], 0)
        self.assertEqual(self.one("SELECT balance_sats FROM addresses WHERE address=?", (B,))["balance_sats"], 49_00000000)

    def test_protocol_state_rolls_back_with_the_block(self):
        sid = self._etch_token(cap=10)
        failing = FailingConn(self.conn, "INSERT INTO transactions", "boom")
        b = block(20, [
            tx("mint1", [out(0.01, C), opret(codec.soqunes_encode_mint(sid))]),
            tx("boom", [out(1.0, D)], coinbase=True),
        ], previous=bhash(19))
        with self.assertRaises(sqlite3.OperationalError):
            chain.process_block(failing, b, 20, self.utxo)
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 0)
        self.assertIsNone(self.one("SELECT 1 FROM soqune_balances WHERE address=?", (C,)))
        self.assertIsNone(self.one("SELECT 1 FROM soqune_applied WHERE txid='mint1'"))

    # helper shared with protocol tests
    def _etch_token(self, name="TKN", premine=0, amount=100_000_000, cap=21, div=0, commit_h=10, reveal_h=16):
        rs = codec.soqunes_encode_etch(name, premine, amount, cap, divisibility=div)
        commit_script = codec.encode_op_return("COMMIT:" + h256(bytes.fromhex(rs)).hex())
        for h in range(1, commit_h):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(commit_h, [tx("commit1", [out(0.1, A), opret(commit_script)], coinbase=True)])
        for h in range(commit_h + 1, reveal_h):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(reveal_h, [tx("reveal1", [out(0.05, B), opret(rs)], inputs=[("commit1", 0)])])
        for h in range(reveal_h + 1, 20):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        row = self.one("SELECT soqune_id FROM soqune_tokens WHERE name=?", (name,))
        return int(row["soqune_id"]) if row else None


# ----------------------------------------------------------------------------
# Cenotaphs / robustness
# ----------------------------------------------------------------------------
class RobustnessTests(Base):
    def test_malformed_message_is_a_cenotaph_not_a_crash(self):
        bad = "6a5d03ffffff"   # OP_RETURN OP_13, push 3 bytes of unterminated LEB128
        self.apply(1, [tx("cb1", [out(1.0, A)], coinbase=True)])
        self.apply(2, [tx("bad1", [out(0.5, B), opret(bad)], inputs=[("cb1", 0)])], previous=bhash(1))
        self.assertEqual(self.count("blocks"), 2)
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_applied WHERE txid='bad1'"))
        self.assertEqual(self.count("soqune_tokens"), 0)

    def test_truncated_tag_without_value_is_cenotaph(self):
        rs = codec.soqunes_encode([codec.SOQUNES_TAG["mint"]])   # tag, no value
        self.apply(1, [tx("cb1", [out(1.0, A)], coinbase=True)])
        self.apply(2, [tx("c1", [out(0.5, B), opret(rs)], inputs=[("cb1", 0)])], previous=bhash(1))
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_applied WHERE txid='c1'"))
        self.assertEqual(self.count("soqune_balances"), 0)

    def test_non_message_op_return_is_ignored(self):
        self.apply(1, [tx("cb1", [out(1.0, A), opret(codec.encode_op_return("hello"))], coinbase=True)])
        self.assertEqual(self.count("soqune_applied"), 0)
        self.assertEqual(self.one("SELECT script_type FROM tx_outputs WHERE txid='cb1' AND vout=1")["script_type"], "nulldata")

    def test_singular_address_field_is_supported(self):
        self.apply(1, [tx("cb1", [out(2.0, A, singular=True)], coinbase=True)])
        self.assertEqual(self.one("SELECT address FROM tx_outputs WHERE txid='cb1'")["address"], A)
        self.assertIn(("cb1", 0), self.utxo)

    def test_reset_chain_clears_protocol_state_too(self):
        AtomicityTests._etch_token(self, cap=5)
        self.assertEqual(self.count("soqune_tokens"), 1)
        chain.reset_chain(self.conn)
        for t in chain.CHAIN_TABLES + chain.PROTOCOL_TABLES:
            self.assertEqual(self.count(t), 0, t)
        self.assertEqual(get_meta(self.conn, "last_height"), "-1")

    def test_large_block_counts(self):
        txs = [tx("cb", [out(1.0, A)], coinbase=True)] + [tx("t%d" % i, [out(0.001, B)]) for i in range(300)]
        self.apply(1, txs)
        self.assertEqual(self.one("SELECT tx_count FROM blocks WHERE height=1")["tx_count"], 301)
        self.assertEqual(self.count("transactions"), 301)
        self.assertEqual(self.one("SELECT utxo_count FROM addresses WHERE address=?", (B,))["utxo_count"], 300)

    def test_to_sats_excludes_change_back_to_sender(self):
        self.apply(1, [tx("cb1", [out(10.0, A)], coinbase=True)])
        self.apply(2, [tx("s1", [out(3.0, B), out(6.9, A)], inputs=[("cb1", 0)])], previous=bhash(1))
        row = self.one("SELECT to_sats, fee_sats, value_out_sats FROM transactions WHERE txid='s1'")
        self.assertEqual(row["to_sats"], 3_00000000)
        self.assertEqual(row["fee_sats"], 10_000000)
        self.assertEqual(row["value_out_sats"], 9_90000000)


# ----------------------------------------------------------------------------
# Soqunes protocol through the block pipeline
# ----------------------------------------------------------------------------
class ProtocolTests(Base):
    _etch_token = AtomicityTests._etch_token

    def test_etch_commit_reveal_creates_token_with_block_tx_id(self):
        sid = self._etch_token(name="ZED", premine=0, amount=5, cap=3)
        self.assertIsNotNone(sid)
        self.assertEqual(sid, (16 << 32) | 0)
        tok = self.one("SELECT * FROM soqune_tokens WHERE soqune_id=?", (sid,))
        self.assertEqual((tok["name"], tok["amount"], tok["cap"], tok["minted"]), ("ZED", 5, 3, 0))
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_events WHERE kind='etch' AND txid='reveal1'"))

    def test_premine_is_credited_to_pointer_output(self):
        sid = self._etch_token(name="PRE", premine=777, amount=1, cap=1)
        bal = self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, B))
        self.assertEqual(bal["balance"], 777)
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_utxos WHERE txid='reveal1' AND soqune_id=?", (sid,)))

    def test_reveal_before_commit_depth_is_rejected(self):
        rs = codec.soqunes_encode_etch("EARLY", 0, 1, 1)
        commit_script = codec.encode_op_return("COMMIT:" + h256(bytes.fromhex(rs)).hex())
        self.apply(1, [tx("commit1", [out(0.1, A), opret(commit_script)], coinbase=True)])
        early = 1 + protocol.COMMIT_DEPTH - 1          # one block short of the required depth
        for h in range(2, early):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(early, [tx("reveal1", [out(0.05, B), opret(rs)], inputs=[("commit1", 0)])])
        self.assertEqual(self.count("soqune_tokens"), 0)
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_applied WHERE txid='reveal1'"))

    def test_reveal_without_commit_is_rejected(self):
        rs = codec.soqunes_encode_etch("NOCOMMIT", 0, 1, 1)
        self.apply(1, [tx("cb1", [out(1.0, A)], coinbase=True)])
        for h in range(2, 9):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(9, [tx("reveal1", [out(0.05, B), opret(rs)], inputs=[("cb1", 0)])])
        self.assertEqual(self.count("soqune_tokens"), 0)

    def test_duplicate_name_is_rejected_first_wins(self):
        sid = self._etch_token(name="DUP", cap=1)
        rs = codec.soqunes_encode_etch("DUP", 0, 9, 9)
        commit_script = codec.encode_op_return("COMMIT:" + h256(bytes.fromhex(rs)).hex())
        self.apply(20, [tx("commit2", [out(0.1, A), opret(commit_script)], coinbase=True)])
        for h in range(21, 26):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(26, [tx("reveal2", [out(0.05, C), opret(rs)], inputs=[("commit2", 0)])])
        self.assertEqual(self.count("soqune_tokens"), 1)
        self.assertEqual(self.one("SELECT amount FROM soqune_tokens WHERE name='DUP'")["amount"], 100_000_000)

    def test_mint_credits_amount_once_per_tx_to_pointer(self):
        sid = self._etch_token(amount=100_000_000, cap=10)
        rs = codec.soqunes_encode_mint(sid)
        self.apply(20, [tx("m1", [out(0.01, C), opret(rs)])])
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 1)
        self.assertEqual(self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, C))["balance"], 100_000_000)
        u = self.one("SELECT * FROM soqune_utxos WHERE txid='m1'")
        self.assertEqual((u["vout"], u["address"], u["units"]), (0, C, 100_000_000))

    def test_mint_pointer_selects_second_output(self):
        sid = self._etch_token(cap=10)
        rs = codec.soqunes_encode([codec.SOQUNES_TAG["mint"], sid, codec.SOQUNES_TAG["pointer"], 1])
        self.apply(20, [tx("m1", [out(0.01, C), out(0.01, D), opret(rs)])])
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D)))
        self.assertIsNone(self.one("SELECT 1 FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, C)))

    def test_mint_cap_is_enforced(self):
        sid = self._etch_token(cap=1)
        rs = codec.soqunes_encode_mint(sid)
        self.apply(20, [tx("m1", [out(0.01, C), opret(rs)])])
        self.apply(21, [tx("m2", [out(0.01, D), opret(rs)])])
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 1)
        self.assertIsNone(self.one("SELECT 1 FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D)))

    def test_mint_respects_height_window(self):
        T = codec.SOQUNES_TAG
        rs = codec.soqunes_encode([T["body"], T["name"], codec.base26_encode("WIN"), T["amount"], 7, T["cap"], 99,
                                  T["height_start"], 22, T["height_end"], 23])
        commit_script = codec.encode_op_return("COMMIT:" + h256(bytes.fromhex(rs)).hex())
        self.apply(1, [tx("commit1", [out(0.1, A), opret(commit_script)], coinbase=True)])
        for h in range(2, 7):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(7, [tx("reveal1", [out(0.05, B), opret(rs)], inputs=[("commit1", 0)])])
        sid = int(self.one("SELECT soqune_id FROM soqune_tokens WHERE name='WIN'")["soqune_id"])
        for h in range(8, 21):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        mint = codec.soqunes_encode_mint(sid)
        self.apply(21, [tx("early", [out(0.01, D), opret(mint)])])   # before start -> ignored
        self.apply(22, [tx("in1", [out(0.01, C), opret(mint)])])     # start height -> ok
        self.apply(23, [tx("in2", [out(0.01, C), opret(mint)])])     # end height -> ok
        self.apply(24, [tx("late", [out(0.01, D), opret(mint)])])    # after end -> ignored
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 2)
        self.assertIsNone(self.one("SELECT 1 FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D)))

    def test_mint_before_height_start_is_ignored(self):
        T = codec.SOQUNES_TAG
        rs = codec.soqunes_encode([T["body"], T["name"], codec.base26_encode("LATE"), T["amount"], 1, T["cap"], 9, T["height_start"], 30])
        commit_script = codec.encode_op_return("COMMIT:" + h256(bytes.fromhex(rs)).hex())
        self.apply(1, [tx("commit1", [out(0.1, A), opret(commit_script)], coinbase=True)])
        for h in range(2, 7):
            self.apply(h, [tx("cb%d" % h, [out(1.0, A)], coinbase=True)])
        self.apply(7, [tx("reveal1", [out(0.05, B), opret(rs)], inputs=[("commit1", 0)])])
        sid = int(self.one("SELECT soqune_id FROM soqune_tokens WHERE name='LATE'")["soqune_id"])
        self.apply(8, [tx("m1", [out(0.01, C), opret(codec.soqunes_encode_mint(sid))])])
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 0)

    def test_mint_of_unknown_token_is_ignored(self):
        self.apply(1, [tx("m1", [out(0.01, C), opret(codec.soqunes_encode_mint(123456))], coinbase=True)])
        self.assertEqual(self.count("soqune_balances"), 0)
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_applied WHERE txid='m1'"))

    def test_transfer_moves_units_via_pointer(self):
        sid = self._etch_token(amount=50, cap=10)
        self.apply(20, [tx("m1", [out(0.01, C), opret(codec.soqunes_encode_mint(sid))])])
        self.apply(21, [tx("x1", [out(0.005, D), opret(codec.soqunes_encode_transfer(0))], inputs=[("m1", 0)])])
        self.assertEqual(self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, C))["balance"], 0)
        self.assertEqual(self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D))["balance"], 50)
        self.assertIsNone(self.one("SELECT 1 FROM soqune_utxos WHERE txid='m1'"))
        self.assertEqual(self.one("SELECT units FROM soqune_utxos WHERE txid='x1'")["units"], 50)
        self.assertIsNotNone(self.one("SELECT 1 FROM soqune_events WHERE kind='transfer' AND txid='x1'"))

    def test_transfer_without_pointer_does_not_move(self):
        sid = self._etch_token(amount=50, cap=10)
        self.apply(20, [tx("m1", [out(0.01, C), opret(codec.soqunes_encode_mint(sid))])])
        self.apply(21, [tx("x1", [out(0.005, D)], inputs=[("m1", 0)])])   # plain spend, no message
        self.assertEqual(self.one("SELECT balance FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, C))["balance"], 50)
        self.assertIsNone(self.one("SELECT 1 FROM soqune_balances WHERE soqune_id=? AND address=?", (sid, D)))

    def test_apply_is_idempotent_per_txid(self):
        sid = self._etch_token(cap=10)
        outs = [(0, 1_000_000, C, "witness_v1_scripthash", None), (1, 0, None, "nulldata", codec.soqunes_encode_mint(sid))]
        protocol.apply(self.conn, "dup", 20, 0, [], outs)
        protocol.apply(self.conn, "dup", 20, 0, [], outs)
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 1)

    def test_apply_never_commits_on_its_own(self):
        sid = self._etch_token(cap=10)
        outs = [(0, 1_000_000, C, "witness_v1_scripthash", None), (1, 0, None, "nulldata", codec.soqunes_encode_mint(sid))]
        protocol.apply(self.conn, "uncommitted", 20, 0, [], outs)
        self.conn.rollback()
        self.assertEqual(self.one("SELECT minted FROM soqune_tokens WHERE soqune_id=?", (sid,))["minted"], 0)


# ----------------------------------------------------------------------------
# Codec edge cases used by the indexer
# ----------------------------------------------------------------------------
class CodecTests(unittest.TestCase):
    def test_leb128_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 2 ** 32, 2 ** 40 + 5):
            ints = codec.soqunes_ints(bytes.fromhex(codec.soqunes_encode([n])))
            self.assertEqual(ints, [n])

    def test_soqunes_ints_rejects_non_message(self):
        self.assertIsNone(codec.soqunes_ints(bytes.fromhex("6a04deadbeef")))
        self.assertIsNone(codec.soqunes_ints(b""))

    def test_soqunes_ints_raises_on_truncated_leb128(self):
        with self.assertRaises(ValueError):
            codec.soqunes_ints(bytes.fromhex("6a5d03ffffff"))

    def test_pushdata1_and_pushdata2_lengths(self):
        many = [1] * 200
        hx = codec.soqunes_encode(many)
        self.assertTrue(hx.startswith("6a5d4d") or hx.startswith("6a5d4c"))
        self.assertEqual(codec.soqunes_ints(bytes.fromhex(hx)), many)

    def test_base26_roundtrip(self):
        for name in ("A", "Z", "TKN", "ZZZZ", "SOQUNES"):
            self.assertEqual(codec.base26_decode(codec.base26_encode(name)), name)

    def test_parse_marks_cenotaph_on_dangling_tag(self):
        p = protocol.parse([codec.SOQUNES_TAG["mint"]])
        self.assertTrue(p["cenotaph"])

    def test_parse_classifies_mint_etch_pointer(self):
        T = codec.SOQUNES_TAG
        self.assertEqual(protocol.parse([T["mint"], 7])["mint"], 7)
        self.assertIsNotNone(protocol.parse([T["name"], 5, T["amount"], 1])["etch"])
        self.assertEqual(protocol.parse([T["pointer"], 2])["pointer"], 2)


# ----------------------------------------------------------------------------
# Mempool watcher
# ----------------------------------------------------------------------------
class FakeRpc:
    def __init__(self, pool, txs):
        self.pool = pool
        self.txs = txs

    def call(self, method, params=None, timeout=None):
        if method == "getrawmempool":
            return self.pool
        if method == "getrawtransaction":
            return self.txs[params[0]]
        raise RuntimeError(method)


class MempoolTests(Base):
    def _tx(self, txid):
        return {"txid": txid, "vin": [{"txid": "p", "vout": 0}], "vout": [out(1.0, A)]}

    def test_sync_inserts_fee_and_diffs_stale(self):
        rpc = FakeRpc({"t1": {"time": 100, "size": 200, "fees": {"base": 0.001}}, "t2": {}}, {"t1": self._tx("t1"), "t2": self._tx("t2")})
        mempool.sync(self.conn, rpc)
        self.assertEqual(self.count("mempool"), 2)
        r = self.one("SELECT * FROM mempool WHERE txid='t1'")
        self.assertEqual((r["fee_sats"], r["size"], r["added_time"], r["value_out_sats"]), (100000, 200, 100, 1_00000000))
        rpc.pool = {"t2": {}}
        mempool.sync(self.conn, rpc)
        self.assertEqual([x["txid"] for x in self.conn.execute("SELECT txid FROM mempool")], ["t2"])
        rpc.pool = {}
        mempool.sync(self.conn, rpc)
        self.assertEqual(self.count("mempool"), 0)

    def test_sync_accepts_list_form_pool(self):
        rpc = FakeRpc(["t1"], {"t1": self._tx("t1")})
        mempool.sync(self.conn, rpc)
        self.assertEqual(self.count("mempool"), 1)

    def test_sync_survives_rpc_errors(self):
        class Broken:
            def call(self, *a, **k):
                raise RuntimeError("down")
        mempool.sync(self.conn, Broken())   # must not raise
        self.assertEqual(self.count("mempool"), 0)


if __name__ == "__main__":
    unittest.main()
