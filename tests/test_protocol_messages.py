"""On-chain ledger: the indexer builds token state from Soqunes messages (chain = source of truth).

Only `OP_RETURN OP_13` messages are applied; the legacy text `SOQ1:` envelope is ignored.
An etch is valid only as a commit/reveal (see tests/test_indexer_deep.py for the full flow);
a plain etch without a prior commit is a cenotaph and creates no token.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import open_test_db  # noqa: E402
from soqindexer import chain, codec, mempool, protocol, runner  # noqa: E402
from soqindexer.accounting import get_meta  # noqa: E402
import os, sys, tempfile, unittest

UNIT = codec.UNIT

def opret_hex(script_hex):
    return {"value": 0, "scriptPubKey": {"type": "nulldata", "hex": script_hex, "addresses": []}}

def p2addr(addr, value=0.0001):
    return {"value": value, "scriptPubKey": {"type": "witness_v1_scripthash", "hex": "5120" + "00"*32, "addresses": [addr]}}

def blk(txs, height=100):
    return {"height": height, "hash": "h%d" % height, "time": 1000, "previousblockhash": "0"*64, "difficulty": 1, "size": 100, "tx": txs}

class ChainLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_test_db(self.tmp.name)

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def n(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()["n"]

    def test_plain_etch_without_commit_is_a_cenotaph(self):
        etch = codec.soqunes_encode_etch("SOQ", 1_000_000_000, 100_000_000, 10)
        mint = codec.soqunes_encode_mint(1)
        utxo = {("fund", 0): (500000000, "sq1sender")}
        chain.process_block(self.conn, blk([
            {"txid": "etch1", "vin": [{"coinbase": "00"}], "vout": [opret_hex(etch)]},
            {"txid": "mint1", "vin": [{"txid": "fund", "vout": 0}], "vout": [opret_hex(mint), p2addr("sq1alice")]},
        ]), 100, utxo)
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM soqune_tokens"), 0)          # no commit -> no token
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM soqune_balances"), 0)        # unknown token -> no mint
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM soqune_applied"), 2)         # both recorded as processed

    def test_text_envelope_is_ignored(self):
        text = codec.encode_op_return("SOQ1:ETCH:SOQ:1000000000:100000000:100000000:10:1")
        chain.process_block(self.conn, blk([
            {"txid": "etch1", "vin": [{"coinbase": "00"}], "vout": [opret_hex(text)]},
        ]), 100, {})
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM soqune_applied"), 0)

    def test_idempotent_reindex(self):
        etch = codec.soqunes_encode_etch("SOQ", 1_000_000_000, 100_000_000, 10)
        b = blk([{"txid": "etch1", "vin": [{"coinbase": "00"}], "vout": [opret_hex(etch)]}])
        first = chain.process_block(self.conn, b, 100, {})
        second = chain.process_block(self.conn, b, 100, {})   # same (height, hash) -> no-op
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM soqune_applied WHERE txid='etch1'"), 1)
        self.assertEqual(self.n("SELECT COUNT(*) AS n FROM transactions"), 1)

if __name__ == "__main__":
    unittest.main()
