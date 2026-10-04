#!/usr/bin/env python3
"""
Audit silang stage3 (Python) terhadap lib/scoring.test.ts (TypeScript).
Kasus 1, 2, 3, 4, 8 dari scoring.test.ts dibentuk ulang sebagai database stage1+stage2,
dijalankan lewat stage3, hasilnya harus sama dengan angka yang diverifikasi di TypeScript.
Jalankan: python3 tests/stage3_cross_check.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE3 = os.path.join(HERE, "..", "scripts", "stage3_compute_scores.py")
FAILS = []


def check(cond, label):
    print(("lolos  " if cond else "GAGAL  ") + label)
    if not cond:
        FAILS.append(label)


def main():
    tmp = tempfile.mkdtemp()
    p1, p2, p3 = (os.path.join(tmp, n) for n in ("s1.db", "s2.db", "s3.db"))
    s1 = sqlite3.connect(p1)
    s1.executescript("""
        create table wallets(address text primary key, first_seen_at text, last_seen_at text, coins_touched integer,
                             total_appearances integer, is_bot_suspect integer, bot_suspect_reason text);
        create table appearances(wallet text, coin_mint text, first_seen_at text, native_sol_volume real, primary key(wallet, coin_mint));""")
    s2 = sqlite3.connect(p2)
    s2.executescript("create table price_points(wallet text, coin_mint text, horizon text, price_usd real, fetched_at text, primary key(wallet, coin_mint, horizon));")

    def add(wallet, mint, entry, h4):
        s1.execute("insert or ignore into wallets values (?,?,?,?,?,0,NULL)", (wallet, "x", "x", 1, 1))
        s1.execute("insert into appearances values (?,?,?,NULL)", (wallet, mint, "2026-01-01T00:00:00+00:00"))
        s2.execute("insert into price_points values (?,?,?,?,?)", (wallet, mint, "entry", entry, "x"))
        s2.execute("insert into price_points values (?,?,?,?,?)", (wallet, mint, "h4", h4, "x"))

    for mint, h4 in zip(["C1", "C2", "C3", "C4", "C5"], [1.5, 1.25, 1.21, 1.10, 0.9]):   # kasus 1
        add("W1", mint, 1.0, h4)
    for mint in ["C1", "C2", "C3"]:                                                        # kasus 2: +400% tapi 3 koin
        add("W2", mint, 1.0, 5.0)
    add("W3", "C1", 1.0, 1.3)                                                              # kasus 3: harga h4 hilang
    add("W3", "C2", 1.0, None)
    add("W4", "C1", 0, 1.0)                                                                # kasus 4: entry 0 / negatif
    add("W4", "C2", -1, 1.0)
    for mint, h4 in zip(["C1", "C2", "C3", "C4", "C5"], [1.2, 1.0, 1.0, 1.0, 1.0]):       # kasus 8: persis +20%
        add("W8", mint, 1.0, h4)
    s1.commit(); s2.commit(); s1.close(); s2.close()

    r = subprocess.run([sys.executable, STAGE3, "--stage1-db", p1, "--stage2-db", p2, "--out", p3], capture_output=True, text=True)
    check(r.returncode == 0, f"stage3 jalan tanpa error {r.stderr[-200:]}")
    rows = {x[0]: x for x in sqlite3.connect(p3).execute("select wallet, coins_scored, hit_rate_h4, avg_return_h4, min_sample_met from wallet_scores")}
    w1 = rows.get("W1")
    check(w1 is not None and w1[1] == 5 and w1[2] == 60 and w1[4] == 1, f"kasus 1: coinsScored=5, hitRate=60, minSample=true (dapat {w1})")
    w2 = rows.get("W2")
    check(w2 is not None and w2[4] == 0 and w2[2] is None and w2[3] is None, f"kasus 2: sampel kurang -> hit/avg NULL (dapat {w2})")
    w3 = rows.get("W3")
    check(w3 is not None and w3[1] == 1, f"kasus 3: harga h4 hilang tidak dihitung (dapat {w3})")
    check("W4" not in rows, "kasus 4: entry 0/negatif -> wallet tidak muncul")
    w8 = rows.get("W8")
    check(w8 is not None and w8[2] == 20, f"kasus 8: persis +20% = hit (epsilon floating point) (dapat {w8})")
    print()
    print("SEMUA AUDIT SILANG LOLOS" if not FAILS else f"{len(FAILS)} GAGAL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
