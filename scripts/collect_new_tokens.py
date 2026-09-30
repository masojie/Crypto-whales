#!/usr/bin/env python3
"""
Crypto-whales — Pengumpul token baru TANPA BIAS (pump.fun)

Kenapa: backtest memakai daftar trending hanya berisi koin yang sudah menang
(survivorship bias), sehingga hit rate 100% tidak berarti apa-apa. Skrip ini mengambil
sampel ACAK dari SEMUA peluncuran token pump.fun (termasuk yang mati), pada jendela waktu
acak di masa lalu, lalu menulis daftar mint untuk extract_onchain.py --token-list.

Cara kerja: untuk tiap jendela waktu acak, baca transaksi program pump.fun dari Helius
(getTransactionsForAddress + filter blockTime), parse lewat Enhanced Transactions, simpan
yang bertipe CREATE. Sampel akhir diambil acak dari semua hasil.

Contoh:
  HELIUS_API_KEY=xxx python3 scripts/collect_new_tokens.py --count 40 --seed 1 --out mints.txt
  HELIUS_API_KEY=xxx python3 scripts/extract_onchain.py --token-list mints.txt --out s1.db
  HELIUS_API_KEY=xxx python3 scripts/stage2_onchain_prices.py --stage1-db s1.db --out s2.db
  python3 scripts/stage3_compute_scores.py --stage1-db s1.db --stage2-db s2.db --out s3.db

Catatan: hanya peluncuran pump.fun (bukan semua token Solana). Umur sampel default 6-30 jam
supaya harga +4 jam sudah ada (+24 jam hanya untuk yang berumur >24 jam).
"""
import argparse, os, random, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_onchain as x

PUMP_FUN = x.PUMP_FUN


def mint_of_create(tx):
    """Mint token dari transaksi CREATE pump.fun. None bila bukan CREATE."""
    if tx.get("type") != "CREATE" or tx.get("source") != "PUMP_FUN":
        return None
    for t in tx.get("tokenTransfers") or []:
        if t.get("mint") and t["mint"] != x.SOL_MINT:
            return t["mint"]
    for ix in tx.get("instructions") or []:
        if ix.get("programId") == PUMP_FUN and ix.get("accounts"):
            return ix["accounts"][0]   # instruksi create: akun pertama = mint
    return None


def creates_in_window(start, span):
    """CREATE dalam [start, start+span] detik. Satu halaman (maks 1000 tx pertama jendela)."""
    res = x.rpc("getTransactionsForAddress",
                [PUMP_FUN, {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 1000,
                            "filters": {"blockTime": {"gte": int(start), "lte": int(start) + span}}}], quiet=False)
    data = res.get("data") if isinstance(res, dict) else None
    if not data:
        return [], 0
    sigs = [s["signature"] for s in data if not s.get("err")]
    out = []
    for tx in x.parse_txs(sigs):
        m = mint_of_create(tx)
        if m:
            out.append({"mint": m, "created": tx["timestamp"]})
    return out, len(sigs)


def main():
    ap = argparse.ArgumentParser(description="Sampel acak token pump.fun baru (tanpa survivorship bias)")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--min-age-hours", type=float, default=6)
    ap.add_argument("--max-age-hours", type=float, default=30)
    ap.add_argument("--window-sec", type=int, default=60)
    ap.add_argument("--max-windows", type=int, default=12)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="mints.txt")
    a = ap.parse_args()
    x.helius_key()
    rng = random.Random(a.seed)
    now = int(time.time())
    lo, hi = now - int(a.max_age_hours * 3600), now - int(a.min_age_hours * 3600) - a.window_sec
    found, seen, windows = [], set(), 0
    while len(found) < a.count * 2 and windows < a.max_windows:
        start = rng.randint(lo, hi)
        windows += 1
        toks, nsig = creates_in_window(start, a.window_sec)
        new = [t for t in toks if t["mint"] not in seen]
        for t in new:
            seen.add(t["mint"])
        found.extend(new)
        print(f"jendela {windows}: {datetime.fromtimestamp(start, timezone.utc):%d %b %H:%M:%S}Z  {nsig} tx -> {len(new)} token CREATE baru")
    if not found:
        print("Tidak ada token ditemukan.", file=sys.stderr)
        return 1
    rng.shuffle(found)
    pick = found[: a.count]
    with open(a.out, "w") as f:
        f.write(f"# sampel acak {len(pick)} dari {len(found)} CREATE pump.fun, {windows} jendela, seed={a.seed}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z\n")
        for t in sorted(pick, key=lambda t: t["created"]):
            f.write(t["mint"] + "\n")
    ages = [(now - t["created"]) / 3600 for t in pick]
    print(f"\n{len(pick)} token ditulis ke {a.out} (umur {min(ages):.1f}-{max(ages):.1f} jam). Panggilan Helius: {x._calls['rpc']} RPC + {x._calls['parse']} parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
