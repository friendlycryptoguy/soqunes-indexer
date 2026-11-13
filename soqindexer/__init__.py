"""soqindexer — Soqucoin chain indexer + Soqunes protocol state machine.

Single-writer indexer that follows a Soqucoin node over JSON-RPC and maintains a relational
index (SQLite by default, PostgreSQL optional): blocks, transactions, inputs/outputs, UTXOs,
address accounting and the Soqunes token protocol state. Reorg-safe via a
per-block undo journal (fork-point rollback) and atomic per-block transactions.
"""
__version__ = "0.1.0"

from .chain import CHAIN_TABLES, PROTOCOL_TABLES, ReorgError, find_fork, load_utxo, process_block, reset_chain, rollback_to  # noqa: F401
from .db import Database, as_db  # noqa: F401
from .protocol import COMMIT_DEPTH, apply, parse, rollback_protocol  # noqa: F401
from .rpc import RpcError, SoqRpc  # noqa: F401
from .schema import ensure  # noqa: F401
