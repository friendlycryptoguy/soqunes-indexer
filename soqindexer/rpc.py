"""Minimal Soqucoin JSON-RPC client with retries/backoff for transport failures.

RPC-level errors (the node answered with `error`) are raised immediately as `RpcError`;
transport problems (connection refused, timeouts, HTTP 5xx) are retried.
"""
import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request


class RpcError(RuntimeError):
    pass


class SoqRpc:
    def __init__(self, url=None, user=None, password=None, retries=3, backoff=0.5):
        self.url = url or os.environ.get("SOQ_RPC_URL", "http://127.0.0.1:33389/")
        user = user if user is not None else os.environ.get("SOQ_RPC_USER", "soqurpc")
        password = password if password is not None else os.environ.get("SOQ_RPC_PASSWORD", "")
        self.auth = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self._id = 0

    def call(self, method, params=None, timeout=60):
        self._id += 1
        body = json.dumps({"jsonrpc": "1.0", "id": self._id, "method": method, "params": params or []}).encode()
        last = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.url, data=body, headers={
                "Content-Type": "application/json", "Authorization": "Basic " + self.auth}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code >= 500:
                    last = exc
                else:
                    try:
                        payload = json.loads(exc.read().decode())
                    except Exception:
                        raise RpcError("RPC HTTP %s" % exc.code)
                    if payload.get("error"):
                        raise RpcError(str(payload["error"].get("message") or payload["error"]))
                    return payload.get("result")
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last = exc
            else:
                if payload.get("error"):
                    raise RpcError(str(payload["error"].get("message") or payload["error"]))
                return payload.get("result")
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise RpcError("RPC transport failed after %d attempts: %s" % (self.retries + 1, last))

    # convenience
    def info(self):
        return self.call("getblockchaininfo")

    def height(self):
        return int(self.call("getblockcount"))

    def block_hash(self, height):
        return self.call("getblockhash", [int(height)])

    def block(self, height):
        return self.call("getblock", [self.block_hash(height), 2])

    def block_by_hash(self, block_hash):
        return self.call("getblock", [block_hash, 2])

    def raw_mempool(self, verbose=True):
        return self.call("getrawmempool", [bool(verbose)])

    def raw_transaction(self, txid, verbose=True, timeout=15):
        return self.call("getrawtransaction", [txid, bool(verbose)], timeout=timeout)
