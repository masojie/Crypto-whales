#!/usr/bin/env python3
"""Tes offline (tanpa jaringan): python3 scripts/test_extract_onchain.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_onchain as x

M, CURVE = "MINT111", "CURVE"
ok = 0
def check(name, cond):
    global ok
    print(("lolos  " if cond else "GAGAL  ") + name)
    if not cond: sys.exit(1)
    ok += 1

def buy(payer, ts, tokens, sol, sender=CURVE, ts_extra=None):
    return {"feePayer": payer, "timestamp": ts, "signature": f"s{ts}",
            "nativeTransfers": [{"fromUserAccount": payer, "toUserAccount": sender, "amount": int(sol * 1e9)},
                                {"fromUserAccount": payer, "toUserAccount": "TIP", "amount": 1513840}],
            "tokenTransfers": [{"mint": M, "fromUserAccount": sender, "toUserAccount": payer, "tokenAmount": tokens}]}

# --- harga & pembeli tunggal
b = x._parse_buys(buy("A", 100, 1000.0, 2.0), M)
check("harga fill = SOL/token (2 SOL / 1000)", len(b) == 1 and abs(b[0]["price_sol"] - 0.002) < 1e-12)
check("volume SOL nyata terisi (tip 1513840 lamport tidak ikut)", b[0]["sol_volume"] == 2.0)
check("bundle_size 1 untuk pembelian biasa", b[0]["bundle_size"] == 1)

# --- dust/airdrop tanpa pembayaran SOL bukan pembelian
dust = {"feePayer": "A", "timestamp": 1, "nativeTransfers": [],
        "tokenTransfers": [{"mint": M, "fromUserAccount": CURVE, "toUserAccount": "A", "tokenAmount": 5.0}]}
check("token diterima tanpa pembayaran SOL -> bukan pembelian", x._parse_buys(dust, M) == [])
check("tx tanpa timestamp -> kosong", x._parse_buys({"feePayer": "A", "tokenTransfers": []}, M) == [])

# --- penjualan bukan pembelian
sell = {"feePayer": "A", "timestamp": 5, "tokenTransfers": [{"mint": M, "fromUserAccount": "A", "toUserAccount": CURVE, "tokenAmount": 10.0}],
        "nativeTransfers": [{"fromUserAccount": CURVE, "toUserAccount": "A", "amount": int(1e9)}]}
check("fee payer yang MENJUAL bukan pembeli", x._parse_buys(sell, M) == [])
check("penjualan tetap memberi sampel harga pasar", len(x.trade_pairs(sell, M)) == 1 and abs(x.trade_pairs(sell, M)[0]["price"] - 0.1) < 1e-12)

# --- bundle: 1 tx, 3 pembeli, pengirim sama
bundle = {"feePayer": "P1", "timestamp": 50, "signature": "sb",
          "nativeTransfers": [{"fromUserAccount": w, "toUserAccount": CURVE, "amount": int(s * 1e9)} for w, s in (("P1", 3.0), ("P2", 4.0), ("P3", 6.0))],
          "tokenTransfers": [{"mint": M, "fromUserAccount": CURVE, "toUserAccount": w, "tokenAmount": t} for w, t in (("P1", 100.0), ("P2", 100.0), ("P3", 100.0))]}
bb = x._parse_buys(bundle, M)
check("bundle: 3 pembeli terdeteksi, bukan cuma fee payer", sorted(v["buyer"] for v in bb) == ["P1", "P2", "P3"])
check("bundle: bundle_size = 3 dan harga tiap wallet benar", all(v["bundle_size"] == 3 for v in bb) and abs({v["buyer"]: v["price_sol"] for v in bb}["P3"] - 0.06) < 1e-12)

# --- wSOL sebagai pembayaran (AMM)
amm = {"feePayer": "A", "timestamp": 9, "nativeTransfers": [],
       "tokenTransfers": [{"mint": M, "fromUserAccount": "POOL", "toUserAccount": "A", "tokenAmount": 500.0},
                          {"mint": x.SOL_MINT, "fromUserAccount": "A", "toUserAccount": "POOL", "tokenAmount": 0.5}]}
check("pembayaran wSOL (AMM) terbaca: 0.5 SOL / 500 = 0.001", abs(x._parse_buys(amm, M)[0]["price_sol"] - 0.001) < 1e-12)

# --- DB: total_appearances, bot, bundle
db = x.init_db(":memory:")
tok = {"mint": M, "created_at": "2026-01-01T00:00:00Z", "created_epoch": 0}
def row(w, ts, price, bs): return {"wallet": w, "first_seen": x.iso(ts), "first_seen_epoch": ts, "sol_volume": 1.0, "price_sol": price, "bundle_size": bs}
x.save(db, tok, [row("BOT", 1, 0.001, 1), row("HUMAN", 2, 0.002, 1), row("INSIDER", 0, 0.0001, 4)], {"BOT": 600, "HUMAN": 2, "INSIDER": 1})
x.recompute_wallets(db)
r = {a: (c, t, bot) for a, c, t, bot in db.execute("select address,coins_touched,total_appearances,is_bot_suspect from wallets")}
check("total_appearances = jumlah beli (600), bukan jumlah koin", r["BOT"][1] == 600)
check("600 beli di 1 koin -> bot suspect", r["BOT"][2] == 1)
check("wallet wajar tidak ditandai", r["HUMAN"][2] == 0)
check("wallet bundle ditandai insider", r["INSIDER"][2] == 1)
e = db.execute("select entry_price_sol, secs_after_creation from appearance_entry where wallet='HUMAN'").fetchone()
check("harga entry per wallet tersimpan (0.002) + detik sejak dibuat (2)", e == (0.002, 2))
print(f"\nSEMUA TEST LOLOS ({ok})")
