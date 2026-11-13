"""Deterministic unit tests for the Soqucoin block chain.

Run from the project root:

    python3 -m unittest discover -s tests -v

No node, wallet, network or Docker is required. Each test builds synthetic
verbose blocks in the same shape returned by Soqucoin `getblock(hash, 2)`.
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




ADDR_A = "sq1testalice000000000000000000000000000"
ADDR_B = "sq1testbob00000000000000000000000000000"
ADDR_C = "sq1testcarol000000000000000000000000000"
ADDR_D = "sq1testdave0000000000000000000000000000"


def out(value, address=None, script_type="witness_v1_scripthash"):
    script = {"type": script_type}
    if address:
        script["addresses"] = [address]
    return {"value": value, "scriptPubKey": script}


def tx(txid, outputs, inputs=None, coinbase=False):
    vin = [{"coinbase": "03deadbeef"}] if coinbase else [
        {"txid": prev_txid, "vout": vout} for prev_txid, vout in (inputs or [])
    ]
    return {"txid": txid, "vin": vin, "vout": outputs}


def block(height, txs, previous="0" * 64, when=None, difficulty=1000.0):
    return {
        "height": height,
        "hash": "b%063d" % height,
        "previousblockhash": previous,
        "time": when if when is not None else 1_700_000_000 + height * 60,
        "size": 500 + height,
        "difficulty": difficulty,
        "tx": txs,
    }


class IndexerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_test_db(self.tmp.name)
        self.utxo = {}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def apply(self, height, txs, previous="0" * 64, difficulty=1000.0):
        b = block(height, txs, previous, difficulty=difficulty)
        chain.process_block(self.conn, b, height, self.utxo)
        return b

    def row(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def rows(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # Block and transaction basics
    # ------------------------------------------------------------------
    def test_genesis_block_is_indexed(self):
        b = self.apply(0, [tx("cb0", [out(50.0, ADDR_A)], coinbase=True)])
        row = self.row("SELECT * FROM blocks WHERE height=0")
        self.assertIsNotNone(row)
        self.assertEqual(row["hash"], b["hash"])
        self.assertEqual(row["tx_count"], 1)
        self.assertEqual(row["size"], 500)

    def test_coinbase_flag_is_stored(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        row = self.row("SELECT is_coinbase,fee_sats FROM transactions WHERE txid='cb1'")
        self.assertEqual(row["is_coinbase"], 1)
        self.assertEqual(row["fee_sats"], 0)

    def test_coinbase_reward_sums_all_outputs(self):
        self.apply(1, [tx("cb1", [out(30.0, ADDR_A), out(20.0, ADDR_B)], coinbase=True)])
        row = self.row("SELECT value_out_sats FROM transactions WHERE txid='cb1'")
        self.assertEqual(row["value_out_sats"], 50_00000000)

    def test_multiple_transactions_in_one_block(self):
        self.apply(1, [
            tx("cb1", [out(50.0, ADDR_A)], coinbase=True),
            tx("spend1", [out(49.0, ADDR_B)], inputs=[("cb1", 0)]),
        ])
        self.assertEqual(self.row("SELECT COUNT(*) n FROM transactions")["n"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM blocks")["n"], 1)

    # ------------------------------------------------------------------
    # Outputs, addresses, UTXOs
    # ------------------------------------------------------------------
    def test_outputs_and_utxos_are_created(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A), out(25.0, ADDR_B)], coinbase=True)])
        self.assertEqual(self.row("SELECT COUNT(*) n FROM tx_outputs")["n"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM utxos")["n"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM utxos WHERE status=0")["n"], 2)
        self.assertIn(("cb1", 0), self.utxo)
        self.assertEqual(self.utxo[("cb1", 0)], (50_00000000, ADDR_A))

    def test_address_balance_and_received_are_credited(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        row = self.row("SELECT * FROM addresses WHERE address=?", (ADDR_A,))
        self.assertEqual(row["balance_sats"], 50_00000000)
        self.assertEqual(row["received_sats"], 50_00000000)
        self.assertEqual(row["sent_sats"], 0)
        self.assertEqual(row["utxo_count"], 1)
        self.assertEqual(row["tx_count"], 1)

    def test_address_tx_history_is_recorded(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        row = self.row("SELECT * FROM address_txs WHERE address=? AND txid='cb1'", (ADDR_A,))
        self.assertEqual(row["received_sats"], 50_00000000)
        self.assertEqual(row["sent_sats"], 0)
        self.assertEqual(row["height"], 1)

    def test_two_outputs_to_same_address_are_aggregated(self):
        self.apply(1, [tx("cb1", [out(20.0, ADDR_A), out(30.0, ADDR_A)], coinbase=True)])
        addr = self.row("SELECT * FROM addresses WHERE address=?", (ADDR_A,))
        self.assertEqual(addr["balance_sats"], 50_00000000)
        self.assertEqual(addr["utxo_count"], 2)
        self.assertEqual(addr["tx_count"], 1)

    def test_addressless_output_is_indexed_but_not_in_utxo_map(self):
        self.apply(1, [tx("cb1", [out(50.0, None), out(10.0, ADDR_A)], coinbase=True)])
        self.assertEqual(self.row("SELECT COUNT(*) n FROM tx_outputs")["n"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM utxos")["n"], 2)
        self.assertNotIn(("cb1", 0), self.utxo)
        self.assertIn(("cb1", 1), self.utxo)

    def test_zero_value_output_is_indexed(self):
        self.apply(1, [tx("cb1", [out(0.0, ADDR_A)], coinbase=True)])
        self.assertEqual(self.row("SELECT value_sats FROM tx_outputs WHERE txid='cb1'")["value_sats"], 0)
        self.assertEqual(self.row("SELECT balance_sats FROM addresses WHERE address=?", (ADDR_A,))["balance_sats"], 0)

    def test_decimal_amounts_use_satoshi_precision(self):
        self.apply(1, [tx("cb1", [out(0.1, ADDR_A), out(0.2, ADDR_B)], coinbase=True)])
        rows = self.rows("SELECT value_sats FROM tx_outputs ORDER BY vout")
        self.assertEqual([r["value_sats"] for r in rows], [10_000000, 20_000000])

    # ------------------------------------------------------------------
    # Spends, fees, sender/recipient accounting
    # ------------------------------------------------------------------
    def test_spend_marks_previous_utxo_spent(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(49.0, ADDR_B)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        row = self.row("SELECT * FROM utxos WHERE txid='cb1' AND vout=0")
        self.assertEqual(row["status"], 1)
        self.assertEqual(row["spent_by_txid"], "spend1")
        self.assertEqual(row["spent_height"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM utxos WHERE status=0")["n"], 1)

    def test_spend_updates_sender_and_recipient_balances(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(49.0, ADDR_B)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        a = self.row("SELECT * FROM addresses WHERE address=?", (ADDR_A,))
        b = self.row("SELECT * FROM addresses WHERE address=?", (ADDR_B,))
        self.assertEqual(a["balance_sats"], 0)
        self.assertEqual(a["sent_sats"], 50_00000000)
        self.assertEqual(a["utxo_count"], 0)
        self.assertEqual(b["balance_sats"], 49_00000000)
        self.assertEqual(b["received_sats"], 49_00000000)

    def test_normal_transaction_fee_is_computed(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(49.9, ADDR_B)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        row = self.row("SELECT * FROM transactions WHERE txid='spend1'")
        self.assertEqual(row["fee_sats"], 10_000000)
        self.assertEqual(row["input_count"], 1)
        self.assertEqual(row["output_count"], 1)

    def test_multiple_inputs_are_resolved(self):
        self.apply(1, [tx("cb1", [out(10.0, ADDR_A), out(20.0, ADDR_B)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(29.5, ADDR_C)], inputs=[("cb1", 0), ("cb1", 1)])], previous="b%063d" % 1)
        rows = self.rows("SELECT * FROM tx_inputs WHERE txid='spend1' ORDER BY vin")
        self.assertEqual([r["prev_address"] for r in rows], [ADDR_A, ADDR_B])
        self.assertEqual([r["prev_value_sats"] for r in rows], [10_00000000, 20_00000000])
        self.assertEqual(self.row("SELECT fee_sats FROM transactions WHERE txid='spend1'")["fee_sats"], 50_000000)

    def test_unresolved_input_has_zero_value(self):
        self.apply(1, [tx("spend1", [out(1.0, ADDR_A)], inputs=[("missing", 3)])])
        inp = self.row("SELECT * FROM tx_inputs WHERE txid='spend1'")
        txrow = self.row("SELECT fee_sats FROM transactions WHERE txid='spend1'")
        self.assertEqual(inp["prev_value_sats"], 0)
        self.assertIsNone(inp["prev_address"])
        self.assertEqual(txrow["fee_sats"], 0)

    def test_sender_history_records_sent_amount(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(50.0, ADDR_B)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        row = self.row("SELECT * FROM address_txs WHERE address=? AND txid='spend1'", (ADDR_A,))
        self.assertEqual(row["sent_sats"], 50_00000000)
        self.assertEqual(row["received_sats"], 0)

    def test_change_output_is_net_not_both_received_and_sent(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(30.0, ADDR_B), out(19.0, ADDR_A)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        a = self.row("SELECT * FROM address_txs WHERE address=? AND txid='spend1'", (ADDR_A,))
        self.assertEqual(a["sent_sats"], 31_00000000)   # net: spent 50, got 19 change back -> 31 out
        self.assertEqual(a["received_sats"], 0)
        b = self.row("SELECT * FROM address_txs WHERE address=? AND txid='spend1'", (ADDR_B,))
        self.assertEqual(b["received_sats"], 30_00000000)
        self.assertEqual(b["sent_sats"], 0)

    # ------------------------------------------------------------------
    # Meta / UTXO reload / reset / difficulty backfill
    # ------------------------------------------------------------------
    def test_block_metadata_and_tip_meta_are_updated(self):
        self.apply(7, [tx("cb7", [out(50.0, ADDR_A)], coinbase=True)])
        meta = {r["key"]: r["value"] for r in self.rows("SELECT * FROM meta")}
        self.assertEqual(meta["last_height"], "7")
        self.assertEqual(meta["last_hash"], "b%063d" % 7)
        self.assertTrue(meta["indexed_at"])

    def test_load_utxo_excludes_spent_and_addressless_outputs(self):
        self.apply(1, [tx("cb1", [out(10.0, ADDR_A), out(20.0, None)], coinbase=True)])
        self.apply(2, [tx("spend1", [out(9.0, ADDR_B)], inputs=[("cb1", 0)])], previous="b%063d" % 1)
        loaded = chain.load_utxo(self.conn)
        self.assertNotIn(("cb1", 0), loaded)
        self.assertNotIn(("cb1", 1), loaded)
        self.assertIn(("spend1", 0), loaded)

    def test_reset_chain_clears_chain_tables(self):
        self.apply(1, [tx("cb1", [out(50.0, ADDR_A)], coinbase=True)])
        chain.reset_chain(self.conn)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM blocks")["n"], 0)
        self.assertEqual(self.row("SELECT COUNT(*) n FROM transactions")["n"], 0)
        self.assertEqual(get_meta(self.conn, "last_height"), "-1")

    def test_difficulty_backfill_updates_recent_zero_rows(self):
        self.apply(1, [tx("cb1", [out(1.0, ADDR_A)], coinbase=True)], difficulty=0)
        self.apply(2, [tx("cb2", [out(1.0, ADDR_A)], coinbase=True)], previous="b%063d" % 1, difficulty=0)
        self.conn.execute("UPDATE blocks SET difficulty=0")
        self.conn.commit()

        class FakeRpc:
            def block(self, height):
                return {"difficulty": 1234.5}

        runner.backfill_difficulty(self.conn, FakeRpc(), limit=2)
        rows = self.rows("SELECT difficulty FROM blocks ORDER BY height")
        self.assertEqual([r["difficulty"] for r in rows], [1234.5, 1234.5])

    def test_difficulty_is_stored_from_block_payload(self):
        self.apply(4, [tx("cb4", [out(50.0, ADDR_A)], coinbase=True)], difficulty=42.25)
        self.assertEqual(self.row("SELECT difficulty FROM blocks WHERE height=4")["difficulty"], 42.25)


    # ------------------------------------------------------------------
    # Token / SOQUNES configuration
    # ------------------------------------------------------------------
    def test_parse_sats_rejects_invalid_values(self):
        self.assertEqual(codec.parse_sats("12.34567890"), 1_234_567_890)
        self.assertEqual(codec.parse_sats("not-a-number"), 0)
        self.assertEqual(codec.parse_sats(None), 0)


if __name__ == "__main__":
    unittest.main()
