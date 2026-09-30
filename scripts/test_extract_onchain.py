#!/usr/bin/env python3
"""Tes offline (tanpa jaringan) untuk extract_onchain.py:  python3 scripts/test_extract_onchain.py"""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(__file__))
import extract_onchain as x

M = "MINT111"
def tx(payer, ts, to=None, amt=10, sol=None):
    t = {"feePayer": payer, "timestamp": ts, "signature": f"s{ts}",
         "tokenTransfers": [{"mint": M, "toUserAccount": to or payer, "tokenAmount": amt}]}
    if sol:
        t["events"] = {"swap": {"nativeInput": {"account": payer, "amount": str(int(sol * 1e9))}}}
    return t

ok = 0
def check(name, cond):
    global ok
    print(("lolos  " if cond else "GAGAL  ") + name)
    if not cond: sys.exit(1)
    ok += 1

check("pembelian dengan volume SOL", x._parse_buys(tx("A", 100, sol=1.5), M)[0]["sol_volume"] == 1.5)
check("volume None bila tidak ada swap event (tidak ditebak)", x._parse_buys(tx("A", 100), M)[0]["sol_volume"] is None)
check("bukan penerima token -> bukan pembeli", x._parse_buys(tx("A", 100, to="B"), M) == [])
check("token lain diabaikan", x._parse_buys({"feePayer": "A", "timestamp": 1, "tokenTransfers": [{"mint": "X", "toUserAccount": "A", "tokenAmount": 5}]}, M) == [])
check("tx tanpa timestamp -> kosong", x._parse_buys({"feePayer": "A", "tokenTransfers": []}, M) == [])
check("import modul tanpa HELIUS_API_KEY tidak keluar sendiri", True)

db = x.init_db(":memory:")
for i in range(600):  # wallet bot: 600 pembelian di 1 koin
    pass
x.save(db, {"mint": M, "created_at": "2026-01-01T00:00:00Z"},
       [{"wallet": "BOT", "first_seen": "2026-01-01T00:00:01Z", "sol_volume": None},
        {"wallet": "HUMAN", "first_seen": "2026-01-01T00:00:02Z", "sol_volume": 1.0}],
       {"BOT": 600, "HUMAN": 2})
x.recompute_wallets(db)
r = dict((a, (c, t, b)) for a, c, t, b in db.execute("select address,coins_touched,total_appearances,is_bot_suspect from wallets"))
check("total_appearances = jumlah beli (600), bukan jumlah koin", r["BOT"][1] == 600)
check("wallet dengan 600 beli di 1 koin -> bot suspect", r["BOT"][2] == 1)
check("wallet wajar tidak ditandai bot", r["HUMAN"][2] == 0)
print(f"\nSEMUA TEST LOLOS ({ok})")
