"""Wire codec for the Soqucoin indexer.

Pure functions, no I/O: satoshi parsing, LEB128, base-26 names, the Soqunes message
(`OP_RETURN OP_13` + LEB128 tag/value integers), OP_RETURN text payloads and a
script→address fallback for witness-v1 (bech32m, HRP `sq`).
"""
import hashlib
from decimal import Decimal, InvalidOperation

UNIT = 100_000_000

SOQUNES_TAG = {"body": 0, "divisibility": 1, "premine": 2, "name": 4, "spacers": 5, "symbol": 6,
               "mint": 20, "amount": 77, "cap": 78, "height_start": 79, "height_end": 80,
               "offset_start": 81, "offset_end": 82, "pointer": 91}


def parse_sats(value):
    """Decimal SOQ (str/float/int) -> integer sats; invalid -> 0. Never uses float math."""
    try:
        return int((Decimal(str(value)) * UNIT).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return 0


def sats_to_soq(value):
    return float(Decimal(int(value or 0)) / UNIT)


def hash256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


# ---- LEB128 --------------------------------------------------------------
def leb128_encode(n):
    n = int(n)
    if n < 0:
        raise ValueError("leb128 expects unsigned")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def leb128_decode(data, i):
    """Return (value, next_index). Raises ValueError on truncation or >63-bit values."""
    result = 0
    shift = 0
    while True:
        if i >= len(data):
            raise ValueError("truncated leb128")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("leb128 too long")


# ---- base-26 names -------------------------------------------------------
def base26_encode(name):
    n = 0
    for ch in str(name).upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError("soqune name must be A-Z")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def base26_decode(n):
    n = int(n)
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


# ---- Soqunes message (OP_RETURN OP_13) ----------------------------------------
def soqunes_encode(ints):
    payload = b"".join(leb128_encode(i) for i in ints)
    if len(payload) <= 75:
        push = bytes([len(payload)]) + payload
    elif len(payload) <= 255:
        push = bytes([0x4C, len(payload)]) + payload
    else:
        push = bytes([0x4D, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return (b"\x6a\x5d" + push).hex()


def soqunes_ints(raw):
    """Decode a Soqunes message script to its LEB128 integers.

    Returns None if the script is not `OP_RETURN OP_13 <push>`; raises ValueError on malformed LEB128.
    """
    if len(raw) < 2 or raw[0] != 0x6A or raw[1] != 0x5D:
        return None
    rest = raw[2:]
    if not rest:
        return []
    op = rest[0]
    if op <= 75:
        length, body = op, rest[1:]
    elif op == 0x4C:
        if len(rest) < 2:
            return None
        length, body = rest[1], rest[2:]
    elif op == 0x4D:
        if len(rest) < 3:
            return None
        length, body = rest[1] | (rest[2] << 8), rest[3:]
    else:
        return None
    payload = body[:length]
    ints, i = [], 0
    while i < len(payload):
        v, i = leb128_decode(payload, i)
        ints.append(v)
    return ints


def soqunes_fields(ints):
    fields = {}
    i = 0
    while i < len(ints):
        tag = ints[i]
        if tag == SOQUNES_TAG["body"]:
            i += 1
            continue
        if i + 1 >= len(ints):
            break
        fields[tag] = ints[i + 1]
        i += 2
    return fields


def soqunes_encode_etch(ticker, supply_units, mint_units, max_mints, divisibility=0):
    ints = [SOQUNES_TAG["body"], SOQUNES_TAG["name"], base26_encode(ticker),
            SOQUNES_TAG["premine"], int(supply_units), SOQUNES_TAG["divisibility"], max(0, min(38, int(divisibility or 0))),
            SOQUNES_TAG["amount"], int(mint_units), SOQUNES_TAG["cap"], int(max_mints)]
    return soqunes_encode(ints)


def soqunes_encode_transfer(out_index):
    return soqunes_encode([SOQUNES_TAG["pointer"], int(out_index)])


def soqunes_encode_mint(token_id=0):
    return soqunes_encode([SOQUNES_TAG["mint"], int(token_id)])


def decode_soqunes(raw):
    ints = soqunes_ints(raw)
    if ints is None:
        return None
    fields = soqunes_fields(ints)
    if SOQUNES_TAG["mint"] in fields:
        return {"protocol": "SOQUNES", "kind": "MINT", "token_id": fields[SOQUNES_TAG["mint"]],
                "quantity": fields.get(SOQUNES_TAG["amount"], 1), "fields": ints}
    if SOQUNES_TAG["name"] in fields:
        return {"protocol": "SOQUNES", "kind": "ETCH", "ticker": base26_decode(fields[SOQUNES_TAG["name"]]),
                "premine": fields.get(SOQUNES_TAG["premine"], 0), "mint_units": fields.get(SOQUNES_TAG["amount"], 0),
                "max_mints": fields.get(SOQUNES_TAG["cap"], 0), "divisibility": fields.get(SOQUNES_TAG["divisibility"], 0),
                "fields": ints}
    if SOQUNES_TAG["pointer"] in fields:
        return {"protocol": "SOQUNES", "kind": "TRANSFER", "pointer": fields[SOQUNES_TAG["pointer"]], "fields": ints}
    return {"protocol": "SOQUNES", "kind": "UNKNOWN", "fields": ints}


# ---- plain OP_RETURN text ------------------------------------------------
def opret_text(script_hex):
    """UTF-8 text of a single-push OP_RETURN (used for `COMMIT:<hash>` markers), else None."""
    try:
        raw = bytes.fromhex(script_hex or "")
        if len(raw) < 2 or raw[0] != 0x6A:
            return None
        rest = raw[1:]
        op = rest[0]
        if op <= 75:
            payload = rest[1:1 + op]
        elif op == 0x4C:
            payload = rest[2:2 + rest[1]]
        else:
            return None
        return payload.decode("utf-8", "ignore")
    except Exception:
        return None


def encode_op_return(text):
    data = str(text).encode("utf-8")
    if len(data) > 80:
        raise ValueError("OP_RETURN payload exceeds 80 bytes")
    push = bytes([len(data)]) + data if len(data) <= 75 else bytes([0x4C, len(data)]) + data
    return (b"\x6a" + push).hex()


# ---- bech32m script -> address fallback ----------------------------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _convert(data, f, t):
    acc = bits = 0
    ret = []
    maxv = (1 << t) - 1
    for value in data:
        acc = (acc << f) | value
        bits += f
        while bits >= t:
            bits -= t
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (t - bits)) & maxv)
    return ret


def bech32m_encode(hrp, ver, prog):
    data = [ver] + _convert(list(prog), 8, 5)
    hrp_exp = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    pm = _polymod(hrp_exp + data + [0, 0, 0, 0, 0, 0]) ^ 0x2BC830A3
    chk = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + chk)


def script_to_address(script_hex, hrp="sq"):
    """Witness v1 32-byte program (`5120<prog>`) -> bech32m address; other scripts -> None."""
    try:
        raw = bytes.fromhex(script_hex or "")
    except ValueError:
        return None
    if len(raw) == 34 and raw[0] == 0x51 and raw[1] == 0x20:
        return bech32m_encode(hrp, 1, raw[2:])
    return None


def address_of(script, hrp="sq"):
    """First address of a verbose scriptPubKey: `addresses[0]` → `address` → decoded from `hex`."""
    if not script:
        return None
    addrs = script.get("addresses") or []
    if addrs:
        return addrs[0]
    if script.get("address"):
        return script["address"]
    return script_to_address(script.get("hex"), hrp)
