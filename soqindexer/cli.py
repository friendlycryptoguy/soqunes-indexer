"""soqindexer command line.

  soqindexer run      [--db URL] [--rpc URL] [--poll N] [--batch N] [--max-rollback N]
  soqindexer mempool  [--db URL] [--rpc URL] [--poll N]
  soqindexer status   [--db URL]
  soqindexer reindex  [--db URL]            # drop chain + protocol state (next `run` rebuilds)
  soqindexer rollback --to HEIGHT [--db URL]

Environment: SOQINDEXER_DB (or SOQUNES_DB), SOQ_RPC_URL, SOQ_RPC_USER, SOQ_RPC_PASSWORD, SOQINDEXER_LOG.
"""
import argparse
import logging
import os
import sys

from . import __version__, chain, mempool, runner, schema
from .accounting import get_meta
from .db import Database, default_url
from .rpc import SoqRpc


def _parser():
    p = argparse.ArgumentParser(prog="soqindexer", description="Soqucoin chain + Soqunes protocol indexer")
    p.add_argument("--version", action="version", version="soqindexer %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, rpc=True):
        sp.add_argument("--db", default=default_url(), help="sqlite path or postgres:// URL")
        sp.add_argument("--log", default=os.environ.get("SOQINDEXER_LOG", "INFO"))
        if rpc:
            sp.add_argument("--rpc", default=os.environ.get("SOQ_RPC_URL", "http://127.0.0.1:33389/"))
            sp.add_argument("--rpc-user", default=os.environ.get("SOQ_RPC_USER", "soqurpc"))
            sp.add_argument("--rpc-password", default=os.environ.get("SOQ_RPC_PASSWORD", ""))

    r = sub.add_parser("run", help="follow the chain"); common(r)
    r.add_argument("--poll", type=int, default=2); r.add_argument("--batch", type=int, default=200)
    r.add_argument("--max-rollback", type=int, default=2000)
    m = sub.add_parser("mempool", help="mempool watcher"); common(m); m.add_argument("--poll", type=int, default=3)
    s = sub.add_parser("status", help="print indexed tip"); common(s, rpc=False)
    x = sub.add_parser("reindex", help="reset chain + protocol state"); common(x, rpc=False)
    b = sub.add_parser("rollback", help="roll back to a height"); common(b, rpc=False); b.add_argument("--to", type=int, required=True)
    return p


def _redact(url):
    """Hide credentials in a DSN for display: postgres://user:***@host/db."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", str(url))


def main(argv=None):
    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log).upper(), logging.INFO),
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s", stream=sys.stdout)
    db = schema.ensure(Database.open(args.db))
    if args.cmd == "status":
        print("db:", _redact(args.db))
        print("last_height:", get_meta(db, "last_height", "-1"), "last_hash:", get_meta(db, "last_hash", ""))
        print("chain_height:", get_meta(db, "chain_height", "?"), "node_synced:", get_meta(db, "node_synced", "?"))
        tok = db.fetchall("SELECT name, soqune_id, minted, cap FROM soqune_tokens ORDER BY soqune_id")
        for t in tok:
            print("token:", t["name"], t["soqune_id"], "minted %s/%s" % (t["minted"], t["cap"]))
        return 0
    if args.cmd == "reindex":
        chain.reset_chain(db); print("chain + protocol state reset; run `soqindexer run` to rebuild"); return 0
    if args.cmd == "rollback":
        utxo = chain.load_utxo(db)
        tip = chain.rollback_to(db, args.to, utxo)
        print("rolled back to", tip); return 0
    rpc = SoqRpc(args.rpc, args.rpc_user, args.rpc_password)
    stop = runner.Stop().install()
    if args.cmd == "mempool":
        mempool.run(db, rpc, poll=args.poll, stop=stop); return 0
    runner.run(db, rpc, poll=args.poll, batch=args.batch, stop=stop, max_rollback=args.max_rollback)
    return 0


if __name__ == "__main__":
    sys.exit(main())
