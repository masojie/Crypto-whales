#!/usr/bin/env python3
"""
Uji scripts/extract_onchain.py dengan RPC TIRUAN (tanpa Helius, tanpa kunci).
Jalankan: python3 tests/test_extract_onchain.py
Test utama = regresi bug "early buyer": kode lama mengembalikan pembeli TERBARU.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

os.environ.setdefault("HELIUS_API_KEY", "dummy")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import extract_onchain as eo

eo.time.sleep = lambda s: None
ORIG_RPC, ORIG_URLOPEN = eo.rpc, urllib.request.urlopen
FAILS = []
T0, MINT = 1_780_000_000, "MintX"


def check(cond, label):
    print(("lolos  " if cond else "GAGAL  ") + label)
    if not cond:
        FAILS.append(label)


def buy_tx(owner, mint=MINT, sol=1.0):
    return {"meta": {"preTokenBalances": [],
                     "postTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": 1000.0}}],
                     "preBalances": [int((sol + 4) * 1e9)], "postBalances": [4_000_000_000]},
            "transaction": {"message": {"accountKeys": [owner]}}}


class Chain:
    """RPC tiruan: transaksi i dibeli B{i} pada T0+10*i. Tanda tangan dikembalikan terbaru -> tertua."""

    def __init__(self, n):
        self.sigs = [{"signature": f"S{i}", "blockTime": T0 + 10 * i, "err": None} for i in reversed(range(n))]
        self.tx_calls = 0
        self.sig_calls = 0

    def __call__(self, method, params, quiet=True):
        if method == "getSignaturesForAddress":
            self.sig_calls += 1
            opts, lst = params[1], self.sigs
            if opts.get("before"):
                lst = lst[[s["signature"] for s in lst].index(opts["before"]) + 1:]
            return lst[: opts.get("limit", 1000)]
        self.tx_calls += 1
        return buy_tx(f"B{int(params[0][1:])}")


class Resp:
    def __init__(self, obj):
        self.b = json.dumps(obj).encode()

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(seq):
    calls = {"n": 0}

    def fake(req, timeout=30):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return Resp(item)
    urllib.request.urlopen = fake
    return calls


def http_err(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def quiet(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = fn(*a)
    return r, buf.getvalue()


def main():
    print("== early buyer = pembeli PERTAMA (regresi bug pembeli terbaru)")
    eo.SIG_PAGE, eo.MAX_SIG_PAGES = 40, 30
    eo.rpc = chain = Chain(100)
    buyers, complete = eo.find_buyers(MINT, 30)
    ws = [b["wallet"] for b in buyers]
    check(ws == [f"B{i}" for i in range(30)], f"30 pembeli tertua B0..B29, urut waktu (dapat {ws[0]}..{ws[-1]}, {len(ws)} wallet)")
    check(complete, "token lengkap dibaca sampai awal")
    check(chain.tx_calls == 30 and chain.sig_calls == 3, f"hemat panggilan: {chain.tx_calls} getTransaction, {chain.sig_calls} halaman tanda tangan")
    check(buyers[0]["first_seen_epoch"] == T0 and buyers[0]["sol_volume"] == 1.0, "waktu dan volume SOL pembeli pertama benar")

    print("== token terlalu ramai ditandai terpotong")
    eo.MAX_SIG_PAGES = 2
    eo.rpc = Chain(100)
    _, complete = eo.find_buyers(MINT, 30)
    check(complete is False, "batas halaman tercapai -> lengkap=False")
    eo.MAX_SIG_PAGES = 30

    print("== transaksi gagal dan tanpa blockTime dilewati")
    eo.rpc = chain = Chain(10)
    chain.sigs[-1]["err"] = {"InstructionError": [0, "x"]}      # S0
    chain.sigs[-2]["blockTime"] = None                          # S1
    buyers, _ = eo.find_buyers(MINT, 30)
    check([b["wallet"] for b in buyers] == [f"B{i}" for i in range(2, 10)] and chain.tx_calls == 8, "B0 (gagal) dan B1 (tanpa blockTime) tidak dihitung, tidak ada panggilan sia-sia")

    print("== rpc(): retry, error permanen, bukan hasil kosong")
    eo.rpc = ORIG_RPC
    calls = fake_urlopen([http_err(429), http_err(429), {"result": [1, 2]}])
    check(eo.rpc("m", []) == [1, 2] and calls["n"] == 3, "429 dua kali lalu sukses")
    calls = fake_urlopen([http_err(429)])
    try:
        eo.rpc("m", [])
        ok = False
    except eo.RpcError:
        ok = True
    check(ok and calls["n"] == 4, f"429 terus -> RpcError setelah 4 percobaan (dapat {calls['n']})")
    calls = fake_urlopen([http_err(403)])
    try:
        eo.rpc("m", [])
        ok = False
    except eo.RpcError as e:
        ok = "HELIUS_API_KEY" in str(e)
    check(ok and calls["n"] == 1, "403 (kunci salah/kuota habis) -> RpcError langsung, bukan diam")
    fake_urlopen([{"error": {"code": -32602, "message": "invalid params"}}])
    try:
        eo.rpc("m", [])
        ok = False
    except eo.RpcError:
        ok = True
    check(ok, "error JSON-RPC -> RpcError")
    urllib.request.urlopen = ORIG_URLOPEN

    print("== database: appearances unik, first_seen tertua menang, bot tidak pura-pura aktif")
    db = eo.init_db(":memory:") if hasattr(eo, "init_db") else None
    check(db is not None, "init_db tersedia")
    if db is not None:
        tok = {"mint": "M1", "symbol": "S", "created_at": "2026-01-01T00:00:00+00:00"}
        late = [{"wallet": "W", "first_seen": "2026-01-02T00:00:00+00:00", "first_seen_epoch": 2, "sol_volume": 1.0}]
        early = [{"wallet": "W", "first_seen": "2026-01-01T00:00:00+00:00", "first_seen_epoch": 1, "sol_volume": 1.0}]
        eo.save(db, tok, late)
        eo.save(db, tok, early)
        row = db.execute("select first_seen_at from appearances where wallet='W' and coin_mint='M1'").fetchone()
        check(row is not None and row[0].startswith("2026-01-01"), f"first_seen tertua menang (dapat {row})")
        for k in range(2, 6):
            eo.save(db, {"mint": f"M{k}", "symbol": "S", "created_at": "x"}, early)
        eo.recompute_wallets(db)
        w = db.execute("select coins_touched, total_appearances, is_bot_suspect from wallets where address='W'").fetchone()
        check(w == (5, 5, 0), f"total_appearances == coins_touched dan tidak ada flag bot palsu (dapat {w})")

    print("== main() end-to-end dengan RPC tiruan")
    eo.SIG_PAGE = 40
    eo.rpc = Chain(100)
    out = os.path.join(tempfile.mkdtemp(), "o.db")
    sys.argv = ["extract_onchain.py", "--token-mint", MINT, "--max-buyers", "5", "--out", out]
    code, _ = quiet(eo.main)
    d = sqlite3.connect(out)
    got = [r[0] for r in d.execute("select wallet from appearances order by first_seen_at")]
    cf = d.execute("select first_seen_at from coins where mint=?", (MINT,)).fetchone()
    check(code in (0, None) and got == [f"B{i}" for i in range(5)], f"5 early buyer tersimpan B0..B4 (dapat {got})")
    check(cf is not None and cf[0].startswith("2026-05-28"), f"coins.first_seen_at = waktu pembeli pertama, bukan waktu sekarang (dapat {cf})")

    print("== main(): berhenti setelah 3 token gagal berturut-turut, tidak ada data palsu")
    def boom(*a, **k):
        raise eo.RpcError("kuota habis")
    eo.rpc = boom
    tf = os.path.join(tempfile.mkdtemp(), "t.txt")
    open(tf, "w").write("\n".join(f"Mint{k}" for k in range(5)))
    out2 = os.path.join(tempfile.mkdtemp(), "o2.db")
    sys.argv = ["extract_onchain.py", "--token-list", tf, "--out", out2]
    _, text = quiet(eo.main)
    n = sqlite3.connect(out2).execute("select count(*) from appearances").fetchone()[0]
    check("BERHENTI" in text and n == 0, f"BERHENTI + 0 appearances (n={n})")

    print()
    print("SEMUA TEST LOLOS" if not FAILS else f"{len(FAILS)} TEST GAGAL:\n  " + "\n  ".join(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
