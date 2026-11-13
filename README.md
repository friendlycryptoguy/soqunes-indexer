# soqindexer

Soqucoin chain indexer + **Soqunes** token protocol state machine.

> **Use at your own risk.** Research & experimental software, provided "as is", with no
> warranty, not audited, and not intended to hold real value.

* **Single writer, reorg-safe.** One process follows a Soqucoin node over JSON-RPC and
  maintains a relational index. Every block is one atomic transaction; a per-block
  **undo journal** enables **fork-point rollback** on reorgs (no full reindex).
* **SQLite by default, PostgreSQL optional.** Zero-ops embedded DB for nodes and tests;
  `postgres://` URL for production query layers (`pip install soqindexer[postgres]`).
* **Consensus-grade robustness.** Parent-hash verification on every block, idempotent
  re-indexing, in-memory UTXO cache restored on failure, malformed protocol messages treated
  as cenotaphs, protocol state rolled back together with chain state.
* **Pure Python, no runtime dependencies.** Tested with synthetic verbose blocks and a fake
  RPC — no node required for the test suite.

---

> **Status: research & experimental — not audited, not for real value.**
> This project is published for **research and educational purposes only**. Use it at your own
> risk; it comes with no warranty of any kind and is not intended to hold real value.
>
> It is released openly: **feel free to fork it, iterate on it and build on it.**
>
> **The users decide.** Protocol parameters and rules — mint terms, fees, the token itself —
> are meant to be governed by the people who actually use them. A change is only legitimate if
> the **users agree / vote** for it; no single party rewrites the protocol under them.

---

## Contents

1. [Install](#install)
2. [Run](#run)
3. [Protocol specification](#protocol-specification)
4. [Indexed tables](#indexed-tables)
5. [Chain ingestion](#chain-ingestion)
6. [Reorg & rollback](#reorg--rollback)
7. [UTXO model](#utxo-model)
8. [Mempool watcher](#mempool-watcher)
9. [Library use](#library-use)
10. [Guarantees & limits](#guarantees--limits)
11. [Tests](#tests)
12. [License](#license)

---

## Install

```bash
pip install soqindexer                # SQLite
pip install "soqindexer[postgres]"    # + PostgreSQL backend
```

## Run

```bash
export SOQ_RPC_URL=http://127.0.0.1:33389/ SOQ_RPC_USER=soqurpc SOQ_RPC_PASSWORD=…
soqindexer run      --db /data/soqindexer.db        # follow the chain
soqindexer mempool  --db /data/soqindexer.db        # unconfirmed tx watcher (separate process)
soqindexer status   --db /data/soqindexer.db
soqindexer rollback --to 42000 --db /data/soqindexer.db
soqindexer reindex  --db /data/soqindexer.db        # drop chain+protocol state; `run` rebuilds
# PostgreSQL
soqindexer run --db postgres://user:pass@host/soqindexer
```

Environment variables: `SOQINDEXER_DB` (or `SOQUNES_DB`), `SOQ_RPC_URL`, `SOQ_RPC_USER`,
`SOQ_RPC_PASSWORD`, `SOQINDEXER_LOG`. `run --max-rollback N` (default 2000).

---

## Protocol specification

The chain is the source of truth. Balances are credited from blocks only — there is no
off-chain ledger, no signed intent, no second database to trust.

> **Credits.** The message design is inspired by the **Runes** protocol
> ([specification](https://docs.ordinals.com/runes), reference implementation
> [`ordinals/ord`](https://github.com/ordinals/ord), CC0-1.0). This project is an
> **independent** implementation and is **not affiliated with or endorsed by** the
> Runes / `ord` authors.

### 1. Message format

A Soqunes message lives in a single `OP_RETURN` output as:

```
OP_RETURN  OP_13  <data push>            (hex prefix: 6a 5d)
```

`OP_13` is the protocol marker (`0x5d`). The data push carries the payload:

| payload length | push opcode | layout |
|---|---|---|
| ≤ 75 | `len` (1 byte) | `[len][payload]` |
| ≤ 255 | `0x4c` `OP_PUSHDATA1` | `4c [len][payload]` |
| ≤ 65535 | `0x4d` `OP_PUSHDATA2` | `4d [len_lo][len_hi][payload]` |

The payload is a flat list of **unsigned LEB128 integers**, interpreted as
`(tag, value, tag, value, …)` pairs. LEB128 is 7 bits per byte, least-significant group
first, high bit = "continuation". Values wider than 63 bits are rejected.

Decoding rules:

* A script that does not start with `6a5d` is **not** a Soqunes message and is ignored.
* The decoder reads the **first** output whose `script_hex` starts with `6a5d`.
* A truncated or over-long LEB128, or an unknown push opcode, makes the message a
  **cenotaph** (see §6).
* Repeated tags overwrite: the last value wins. Unknown tags are ignored.
* Tag `0` (`body`) is a standalone flag and consumes no value. Every other tag consumes
  exactly one value.

### 2. Tags

| Tag | Field | Notes |
|---|---|---|
| `0` | body flag | standalone, no value |
| `1` | divisibility | clamped to 0–38 |
| `2` | premine | units credited at etch time |
| `4` | name | base-26 A–Z (`A`=1 … `Z`=26, big-endian) |
| `5` | spacers | bitmask of gaps in the display name |
| `6` | symbol | single code point |
| `20` | mint | value = `soqune_id` |
| `77` | amount | units minted **per** mint transaction |
| `78` | cap | maximum number of mints |
| `79` | height_start | mint window start (inclusive, `0` = open) |
| `80` | height_end | mint window end (inclusive, `0` = open) |
| `81` | offset_start | relative window start (`0` = open) |
| `82` | offset_end | relative window end (`0` = open) |
| `91` | pointer | index into the **address-output list** (see §4) |

### 3. Etch = commit / reveal

An etch is two transactions. Copying a message from the mempool is therefore useless: only
the holder of the commit output can reveal it.

**Commit (tx 1).** Pays a fresh address *and* carries a plain-text marker in the same
transaction:

```
OP_RETURN  "COMMIT:<HASH256(message_script_bytes)>"
```

`HASH256` is **double SHA-256** of the raw message script bytes (`6a5d…`), *not* of the hex
string. The marker is 7 + 64 = 71 bytes, so it fits in one push.

**Reveal (tx 2 = the etch).** Spends the commit output and carries the actual message. It is
valid only if all three hold:

1. the reveal spends an output of a tx that carries the matching `COMMIT:` marker, and
2. that commit tx is confirmed and **≥ `COMMIT_DEPTH` (6) blocks** old, and
3. the message decodes and carries a non-empty name.

Otherwise it is a **cenotaph** — recorded as applied, no soqune created.

On success:

```
soqune_id = (block_height << 32) | tx_index
```

* `tx_index` is the transaction's position inside the block.
* Names are unique and case-normalised to uppercase; **first valid etch wins**, later etches
  with the same name are ignored.
* `premine` units are credited to the pointer target (see §4).
* An `etch` event is written (`address` = the name, `units` = 0).

### 4. Output targeting (pointer)

Every credit needs a destination output. The target list is built from the transaction's
outputs that **have an address**, in output order:

```python
targets = [(vout, address) for each output with an address]
```

* `pointer` (`tag 91`) selects `targets[pointer]`.
* Out of range, or no pointer → `targets[0]` (the first address output).
* If there is no address output at all → no credit, the message has no effect.

`pointer` indexes the **address-output list**, not the raw vout. A transaction with an
`OP_RETURN` first therefore still has its first address output at pointer `0`.

### 5. Mint

Tag `20` carries the target `soqune_id`. The minted quantity is the token's own
`amount` field — the message does not restate it.

Accepted when:

* the token exists,
* `amount > 0`,
* the height window allows it (`height_start ≤ h ≤ height_end`, `0` = open),
* `cap == 0` or `minted + 1 ≤ cap`,
* the pointer target resolves to an address.

Effects: `minted += 1`, `amount` units credited to the pointer target, one token UTXO row
created, one `mint` event written. **At most one mint per transaction.**

### 6. Transfer

There is **no transfer opcode**. Units move by spending the token UTXO with a `pointer`
message:

1. Every input's token UTXOs are looked up and summed per `soqune_id`; each consumed UTXO
   is journaled into `soqune_undo_utxos` so a rollback can restore it.
2. The summed units are credited to the pointer target and a new token UTXO row is created.
3. A `transfer` event is written.

If the transaction spends no token UTXO, or the pointer target has no address, nothing moves.

### 7. Cenotaphs

A cenotaph is a message that decodes but has no protocol effect. It is written to
`soqune_applied` so it is never re-processed, and **never aborts a block**.

| Condition | Result |
|---|---|
| malformed/truncated LEB128 or unknown push opcode | cenotaph |
| odd number of trailing integers (dangling tag) | cenotaph |
| `mint` for an unknown `soqune_id` | no effect |
| `etch` without a non-empty name | no effect |
| `etch` with an already-etched name | no effect |
| `etch` whose commit marker is missing/too young | no effect |
| transfer whose pointer target has no address | no effect |

`soqune_applied(txid)` is the idempotency guard: a txid is applied at most once, and the
row carries the height so rollback can clear it.

### 8. Encoding examples

Etch `DEMO` — premine `2 100 000 000 000 000`, 8 decimals, `100 000 000` units per mint,
cap `21 000 000`:

```
script : 6a5d1a0004b5c2040280809dd085bedd0301084d80c2d72f4ec0de810a
ints   : [0, 4, 74037, 2, 2100000000000000, 1, 8, 77, 100000000, 78, 21000000]
fields : {0:body, 4:name=74037 ("DEMO"), 2:premine, 1:divisibility=8, 77:amount, 78:cap}
```

Mint of `soqune_id` `4 294 967 296 001` (height 1000, tx 1):

```
script : 6a5d071481808080807d
ints   : [20, 4294967296001]
```

Transfer with `pointer = 1`:

```
script : 6a5d025b01
ints   : [91, 1]
```

Commit marker for the etch script above:

```
script : 6a47434f4d4d49543a34663762656161326530326135353261666363306334656539666661653238396632626139613332323837626163313330306439656161363935396463313532
text   : COMMIT:4f7beaa2e02a552afcc0c4ee9ffae289f2ba9a32287bac1300d9eaa6959dc152
```

Round-trip helpers in `soqindexer.codec`: `soqunes_encode(ints)`, `soqunes_ints(script_bytes)`,
`soqunes_fields(ints)`, `soqunes_encode_etch(...)`, `soqunes_encode_mint(id)`,
`soqunes_encode_transfer(vout)`, `decode_soqunes(script_bytes)`, `encode_op_return(text)`,
`opret_text(script_hex)`, `base26_encode/decode`, `hash256`.

### 9. Semantics summary

| Message | Trigger | Effect |
|---|---|---|
| etch | tag `4` present + valid commit | create token, credit premine |
| mint | tag `20` present | `minted += 1`, credit `amount` |
| transfer | tag `91` present and token UTXOs spent | move units to pointer target |
| cenotaph | malformed / dangling tag | mark applied, no effect |

---

## Indexed tables

| Table | Content |
|---|---|
| `blocks`, `transactions`, `tx_inputs`, `tx_outputs` | Chain data; `transactions.to_sats` = value to non-input addresses |
| `utxos` | Outpoint state (`status` 0/1, spend height/time) |
| `addresses`, `address_txs` | Balance / received / sent / counts and per-address history |
| `block_undo` | Per-block undo journal (spent outpoints + address deltas) |
| `soqune_tokens`, `soqune_balances`, `soqune_utxos`, `soqune_events`, `soqune_applied`, `soqune_undo_utxos` | Soqunes protocol state (height-stamped, rollback-able) |
| `mempool` | Unconfirmed transactions (from `soqindexer mempool`) |
| `meta` | `last_height`, `last_hash`, `chain_height`, `node_synced`, `indexed_at` |

The consuming application reads these tables; it must not write to them (single writer).
Application tables (orders, keys, wallets) belong to the app and are never touched by a
chain rebuild. Schema changes are **additive only**.

### Protocol tables

```sql
soqune_tokens(soqune_id PK, number, block, tx, name UNIQUE, divisibility, premine,
              amount, cap, height_start, height_end, offset_start, offset_end,
              symbol, spacers, etched_height, minted)
soqune_balances(soqune_id, address, balance, PK(soqune_id, address))
soqune_utxos(soqune_id, txid, vout, address, units, height, PK(txid, vout, soqune_id))
soqune_undo_utxos(height, txid, soqune_id, src_txid, src_vout, address, units, src_height)
soqune_events(id, kind, soqune_id, address, units, txid, height, created_at)   -- kind: etch|mint|transfer
soqune_applied(txid PK, applied_at, height)
```

---

## Chain ingestion

Per transaction, in block order:

| Step | Result |
|---|---|
| Coinbase detection | `vin[0].coinbase` → `transactions.is_coinbase=1`, fee 0 |
| Inputs | resolve prevout in the in-memory UTXO map → `prev_value_sats`, `prev_address` |
| Spends | mark `tx_outputs`/`utxos` spent, record spend height/time, debit sender |
| Outputs | decimal SOQ → integer sats via `Decimal`; address resolution `addresses[0]` → `address` → decode `5120<prog>` to bech32m (HRP `sq`) |
| Fees | `max(0, sum(inputs) − sum(outputs))`, 0 for coinbase |
| `to_sats` | output value excluding change back to the tx's own input addresses |
| Address history | one `address_txs` row per address/tx with received/sent totals |
| Protocol | `protocol.apply(conn, txid, height, tx_index, inputs, outputs)` — no commit |

The protocol step runs **inside** the block transaction, so chain rows and token state commit
or roll back together. Difficulty is stored per block; recent zero values are backfilled for
hashrate charts.

---

## Reorg & rollback

* **Detection:** before indexing block *h*, the indexer checks
  `previousblockhash == hash(h−1)`. A mismatch — or a different hash at an already indexed
  height — raises `ReorgError`.
* **Recovery:** find the highest indexed height the node still agrees with (the *fork point*)
  and roll back to it through the per-block undo journal. Reorgs deeper than
  `--max-rollback` (default 2000) fall back to a full `reindex`.
* **What is rolled back:** chain tables *and* protocol state. `rollback_protocol(db, fork)`:
  1. delete token UTXOs created above the fork and debit their balances;
  2. restore token UTXOs consumed above the fork from `soqune_undo_utxos` and re-credit;
  3. decrement `soqune_tokens.minted` by the mint events above the fork;
  4. delete tokens etched above the fork, then their events and `soqune_applied` rows;
  5. drop zero balances.
* Application tables are never touched by a chain rebuild.
* Manual: `soqindexer rollback --to <height>`; `soqindexer reindex`.

---

## UTXO model

`utxos` is the canonical outpoint state; `tx_outputs` keeps every output ever created.

```sql
utxos(
  txid, vout, value_sats, address,
  created_height, created_time,
  spent_by_txid, spent_height, spent_time,
  status  -- 0 = unspent, 1 = spent
)
```

* Addressless outputs are indexed but excluded from the address→UTXO map.
* Zero-value outputs are indexed.
* Unresolved inputs record zero value and no previous address.
* `addresses.balance_sats` is maintained incrementally (with undo), not derived per query.
* The UTXO API returns `utxos[]`, `total`, `total_value_sats`, `offset`, `limit` and a `stats`
  object (`total`, `unspent`, `spent`, `unspent_value_sats`, `spent_value_sats`).

Token UTXOs (`soqune_utxos`) are separate from SOQ UTXOs: one row per
`(txid, vout, soqune_id)` carrying the unit balance that lives on that outpoint.

---

## Mempool watcher

`soqindexer mempool` is a separate process that keeps the `mempool` table in sync. It fetches
`getrawmempool` + `getrawtransaction` for **new txids only** (diff against the table), stores
size, fee, value and counts, and refreshes fee/size on rows whose values changed. Fee is read
from the entry's `fee` field (SOQ), falling back to `fees.base`, and converted to sats with
`Decimal`.

---

## Library use

```python
from soqindexer import Database, ensure, process_block, rollback_to, load_utxo, SoqRpc

db = ensure(Database.sqlite("index.db"))
rpc = SoqRpc("http://127.0.0.1:33389/", "soqurpc", "…")
utxo = load_utxo(db)
process_block(db, rpc.block(1000), 1000, utxo)   # atomic; raises ReorgError on parent mismatch
rollback_to(db, 990, utxo)                        # undo blocks 991..tip (chain + protocol)
```

Any function also accepts a raw `sqlite3.Connection`. Protocol-only entry points:
`protocol.parse(ints)`, `protocol.apply(conn, txid, height, tx_index, inputs, outputs)`,
`protocol.rollback_protocol(conn, fork)`, `protocol.COMMIT_DEPTH`.

`inputs` is an iterable of `(prev_txid, vout)`; `outputs` is an iterable of
`(vout, value_sats, address, script_type, script_hex)`.

---

## Guarantees & limits

* One block = one DB transaction; failure → rollback + UTXO cache restored → safe retry.
* `process_block` is idempotent for an already indexed `(height, hash)`.
* Reorgs: rollback to the fork point within `--max-rollback` (default 2000) blocks, else full reindex.
* SQLite allows one writer; run exactly one `soqindexer run` per database. Readers never block (WAL).
* Concurrent first-time schema creation is serialized with a PostgreSQL advisory lock.

Known limitations (by design):

* **No edicts.** Units always move as a whole UTXO to a single pointer target.
* **No burn on pointer-less spends.** A token UTXO spent without a pointer message stays
  assigned until a pointer transfer moves it.
* Unresolved inputs (prevout unknown to the index) record `fee_sats=0` / `prev_address=NULL`.
* The legacy `SOQ1:` plain-`OP_RETURN` text envelope is **not** part of the indexer; only
  `OP_RETURN OP_13` messages create protocol state.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

Synthetic verbose blocks + a fake RPC + temporary SQLite databases — **no node required**.
Covers chain linkage and reorg detection, exact fork-point rollback, atomic failure injection,
protocol flows (commit/reveal, cap, height window, pointer, transfer), codec edge cases,
mempool diffing and RPC retry policy. Set `SOQINDEXER_TEST_DB=postgres://…` to run the same
suite against PostgreSQL (CI does).

---

## License

MIT
