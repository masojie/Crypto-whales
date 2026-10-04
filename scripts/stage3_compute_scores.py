#!/usr/bin/env python3
"""
Crypto-whales — Tahap 3: hitung skor wallet dari appearances + harga
========================================================================

SKRIP INI MURNI BACA (stage1 + stage2, keduanya read-only) DAN MENULIS SATU
file baru (default crypto_whales_stage3.db). Tidak ada file lain yang disentuh.

LOGIKA SKOR DI SINI SENGAJA DITULIS SUPAYA IDENTIK DENGAN lib/scoring.ts:
  - MIN_SAMPLE = 5       (sama persis dengan scoring.ts)
  - HIT_THRESHOLD_PCT = 20  (sama persis)
  - FLOAT_EPSILON = 1e-9    (sama persis — ini yang memperbaiki bug ambang
    batas floating-point yang ditemukan saat testing scoring.ts: 1.0->1.2
    bisa terhitung 19.999999999999996, bukan 20.0 tepat)

Ini BUKAN kebetulan sama. Ini SENGAJA disalin nilai konstantanya dari
lib/scoring.ts supaya tidak ada dua "kebenaran" berbeda soal kapan sebuah
return dianggap "hit" — kalau konstanta ini pernah diubah di scoring.ts,
angka di sini WAJIB diubah mengikuti, atau kedua sistem akan diam-diam
menghitung skor yang berbeda untuk data yang sama.

Cara verifikasi bahwa Python ini konsisten dengan TypeScript:
    lihat tests/stage3_cross_check.py (jalankan: python3 tests/stage3_cross_check.py) — men-derive skenario yang IDENTIK,
    dengan lib/scoring.test.ts (kasus 1, 2, 4, 8), lalu membuktikan angka
    yang dihasilkan stage3 ini SAMA PERSIS dengan angka yang sudah
    diverifikasi manual di scoring.test.ts.

Pakai (Termux, setelah stage1 dan stage2 selesai/cukup lengkap):
    python3 stage3_compute_scores.py --stage1-db crypto_whales_stage1.db \
                                      --stage2-db crypto_whales_stage2.db \
                                      --out crypto_whales_stage3.db

Bisa dijalankan ULANG kapan saja (misalnya setelah stage2 dapat lebih
banyak koin) — skor dihitung ULANG DARI NOL setiap kali, bukan diakumulasi.
Ini SENGAJA: recompute penuh selalu bisa diverifikasi ulang dari data
mentah, sedangkan akumulasi bertahap gampang drift (lihat catatan di
supabase/migrations/0001_init.sql soal alasan wallet_scores direcompute).
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

MIN_SAMPLE = 5
HIT_THRESHOLD_PCT = 20
FLOAT_EPSILON = 1e-9


def compute_return_pct(entry_price: float | None, price_h4: float | None) -> float | None:
    """IDENTIK dengan computeReturnPct() di lib/scoring.ts. Mengembalikan
    None (bukan 0) kalau data tidak lengkap — 0% return dan 'tidak ada
    data' harus dibedakan, sama seperti alasan di scoring.ts."""
    if entry_price is None or price_h4 is None:
        return None
    if entry_price <= 0:
        return None
    return ((price_h4 - entry_price) / entry_price) * 100


def main() -> int:
    parser = argparse.ArgumentParser(description="Hitung skor wallet dari stage1 (appearances) + stage2 (harga).")
    parser.add_argument("--stage1-db", required=True)
    parser.add_argument("--stage2-db", required=True)
    parser.add_argument("--out", default="crypto_whales_stage3.db")
    parser.add_argument("--top", type=int, default=15, help="tampilkan N wallet teratas di ringkasan")
    args = parser.parse_args()

    print(f"Membuka {args.stage1_db} dan {args.stage2_db} (keduanya read-only) ...")
    stage1 = sqlite3.connect(f"file:{args.stage1_db}?mode=ro", uri=True)
    stage1.execute("PRAGMA query_only = ON;")
    stage2 = sqlite3.connect(f"file:{args.stage2_db}?mode=ro", uri=True)
    stage2.execute("PRAGMA query_only = ON;")

    import os
    fresh = not os.path.exists(args.out)
    out = sqlite3.connect(args.out)
    out.executescript("""
        create table if not exists wallet_scores (
            wallet          text primary key,
            coins_scored    integer not null,
            hit_rate_h4     real,
            avg_return_h4   real,
            min_sample_met  integer not null,
            is_bot_suspect  integer not null,
            computed_at     text not null
        );
    """)
    if not fresh:
        print(f"{args.out} sudah ada — isi wallet_scores akan DITIMPA dengan hasil recompute penuh (by design, lihat docstring).")
        out.execute("delete from wallet_scores;")
    out.commit()

    # Muat semua price_points dari stage2 ke memori sebagai lookup cepat.
    # Untuk 901 koin x ~5 wallet rata-rata x 4 horizon, ini jauh di bawah
    # jutaan baris — aman dimuat penuh ke memori Python di HP.
    print("Memuat price_points dari stage2 ...")
    price_lookup: dict[tuple[str, str, str], float | None] = {}
    for wallet, coin_mint, horizon, price_usd in stage2.execute(
        "select wallet, coin_mint, horizon, price_usd from price_points where horizon in ('entry','h4')"
    ):
        price_lookup[(wallet, coin_mint, horizon)] = price_usd
    print(f"  {len(price_lookup)} titik harga dimuat.")

    print("Memuat wallet + status bot-suspect dari stage1 ...")
    bot_suspect_by_wallet = {
        addr: bool(flag) for addr, flag in stage1.execute("select address, is_bot_suspect from wallets")
    }

    print("Menghitung skor per wallet ...")
    returns_by_wallet: dict[str, list[float]] = {}
    coins_scored_by_wallet: dict[str, int] = {}

    total_appearances = 0
    skipped_no_price_data = 0
    for wallet, coin_mint in stage1.execute("select wallet, coin_mint from appearances"):
        total_appearances += 1
        entry_price = price_lookup.get((wallet, coin_mint, "entry"))
        price_h4 = price_lookup.get((wallet, coin_mint, "h4"))

        ret = compute_return_pct(entry_price, price_h4)
        if ret is None:
            skipped_no_price_data += 1
            continue

        coins_scored_by_wallet[wallet] = coins_scored_by_wallet.get(wallet, 0) + 1
        returns_by_wallet.setdefault(wallet, []).append(ret)

    print(f"  Total appearances diperiksa: {total_appearances}")
    print(f"  Dilewati (data harga tidak lengkap): {skipped_no_price_data}")
    print(f"  Wallet dengan minimal 1 koin ternilai: {len(returns_by_wallet)}")

    rows = []
    min_sample_met_count = 0
    for wallet, returns in returns_by_wallet.items():
        coins_scored = coins_scored_by_wallet[wallet]
        min_sample_met = coins_scored >= MIN_SAMPLE

        if min_sample_met:
            hits = sum(1 for r in returns if r >= HIT_THRESHOLD_PCT - FLOAT_EPSILON)
            hit_rate_h4 = (hits / len(returns)) * 100
            avg_return_h4 = sum(returns) / len(returns)
            min_sample_met_count += 1
        else:
            hit_rate_h4 = None
            avg_return_h4 = None

        rows.append((
            wallet,
            coins_scored,
            hit_rate_h4,
            avg_return_h4,
            int(min_sample_met),
            int(bot_suspect_by_wallet.get(wallet, False)),
            datetime.now(timezone.utc).isoformat(),
        ))

    out.executemany(
        """insert into wallet_scores
           (wallet, coins_scored, hit_rate_h4, avg_return_h4, min_sample_met, is_bot_suspect, computed_at)
           values (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    out.commit()

    print(f"\n{min_sample_met_count} wallet lolos MIN_SAMPLE (>={MIN_SAMPLE} koin ternilai) — skor ini yang layak dipercaya.")
    print(f"{len(rows) - min_sample_met_count} wallet punya skor tapi BELUM cukup sampel (hit_rate/avg_return = NULL).")

    bot_and_qualified = sum(
        1 for r in rows if r[4] == 1 and r[5] == 1
    )
    if bot_and_qualified:
        print(f"PERHATIAN: {bot_and_qualified} wallet lolos MIN_SAMPLE TAPI juga ditandai is_bot_suspect dari stage1.")
        print("  Ini bukan error — artinya wallet itu sering muncul di banyak koin BERBEDA (bukan 1-2 koin saja,")
        print("  karena is_bot_suspect butuh <=3 koin unik). Cek manual sebelum dipercaya sebagai alpha wallet.")

    all_returns = [r for rets in returns_by_wallet.values() for r in rets]
    if all_returns:
        base_hits = sum(1 for r in all_returns if r >= HIT_THRESHOLD_PCT - FLOAT_EPSILON)
        print(f"\nBase rate: {base_hits}/{len(all_returns)} pasangan (wallet,koin) naik >= {HIT_THRESHOLD_PCT}% dalam 4 jam = {100 * base_hits / len(all_returns):.1f}%")
    qualified = sorted((r for r in rows if r[4] == 1), key=lambda r: (-r[2], -r[1]))
    print(f"\nTop {min(args.top, len(qualified))} wallet (hit rate +4j, minimal {MIN_SAMPLE} koin ternilai):")
    for r in qualified[: args.top]:
        print(f"  {r[0]}  koin={r[1]:3d}  hit={r[2]:5.1f}%  rata2={r[3]:+8.1f}%  bot_suspect={'ya' if r[5] else 'tidak'}")

    print(f"\nSelesai. Ditulis ke {args.out}")
    print("\nCATATAN: skor ini HANYA sebaik data stage2. Kalau stage2 belum")
    print("selesai memproses semua koin, banyak wallet akan punya coins_scored")
    print("rendah dan min_sample_met=False. Jalankan ulang skrip ini kapan saja")
    print("setelah stage2 mendapat lebih banyak data — recompute penuh, aman diulang.")

    stage1.close()
    stage2.close()
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
