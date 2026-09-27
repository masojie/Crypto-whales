#!/usr/bin/env python3
"""
Crypto-whales — Tahap 1: ekstraksi & dedup dari backup SCIA
=============================================================

SKRIP INI BACA-SAJA. Dibuka dengan sqlite3 URI mode=ro, jadi tidak
pernah menulis ke file backup. Output: SATU file baru (default
crypto_whales_stage1.db).

Kenapa langkah ini perlu (dari audit sebelumnya):
  - whale_appearances punya 1.199.138 baris tapi cuma 192.775 wallet
    unik -> satu (wallet, koin) dicatat ULANG tiap scan (~10 detik).
    Di sini diambil cuma kemunculan PERTAMA per (wallet, koin).
  - ~99% volume_sol = 10.0 (nilai tebakan SCIA, bukan data asli).
    Kolom ini dicatat apa adanya sebagai referensi, TIDAK dipakai skor.
  - Ada wallet yang muncul 13.000+x tapi cuma di 1-2 koin -> pola
    bot/sniper. Ditandai is_bot_suspect, BUKAN dibuang, supaya kamu
    bisa cek sendiri.

Pakai (Termux):
    python3 stage1_extract.py --db ~/scia_memory_BACKUP_24SEP.db

Output berisi tabel: wallets, coins, appearances (sudah dedup).
TIDAK mengambil harga eksternal apa pun — itu tahap 2 terpisah
(butuh internet + API pihak ketiga yang perlu diuji sendiri).
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

LOW_COIN_COUNT = 3           # sama dengan lib/scoring.ts detectBotSuspect
HIGH_APPEARANCE_COUNT = 500  # sama dengan lib/scoring.ts detectBotSuspect


def open_readonly(path: str) -> sqlite3.Connection:
    """Buka SQLite read-only murni. Kalau file tak ada/tak valid,
    error dibiarkan tampil apa adanya supaya pesannya jelas."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON;")  # lapisan kedua: cegah tulis tak sengaja
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description="Ekstrak & dedup data whale dari backup SCIA (read-only).")
    parser.add_argument("--db", required=True, help="Path ke file backup scia_memory_BACKUP_*.db")
    parser.add_argument("--out", default="crypto_whales_stage1.db", help="Path file output (default: crypto_whales_stage1.db)")
    args = parser.parse_args()

    print(f"Membuka {args.db} dalam mode READ-ONLY ...")
    try:
        src = open_readonly(args.db)
    except sqlite3.OperationalError as e:
        print(f"GAGAL membuka database: {e}", file=sys.stderr)
        print("Pastikan path benar dan file adalah database SQLite yang valid.", file=sys.stderr)
        return 1

    # Cek dulu tabel yang diharapkan ada, supaya error-nya jelas kalau struktur beda
    try:
        cols = {row[1] for row in src.execute("PRAGMA table_info(whale_appearances);")}
    except sqlite3.OperationalError as e:
        print(f"GAGAL membaca skema whale_appearances: {e}", file=sys.stderr)
        return 1

    expected_cols = {"wallet", "coin_symbol", "coin_mint", "volume_sol", "appeared_at"}
    missing = expected_cols - cols
    if missing:
        print(f"PERINGATAN: kolom yang diharapkan tidak ditemukan di whale_appearances: {missing}", file=sys.stderr)
        print("Skema database ini mungkin berbeda dari yang diasumsikan skrip ini. Berhenti demi keamanan.", file=sys.stderr)
        return 1

    print("Skema whale_appearances cocok dengan yang diharapkan. Lanjut.")

    # Siapkan output. Kalau file sudah ada, kita tolak menimpa tanpa sepengetahuan
    # pengguna — lebih aman minta hapus manual daripada diam-diam menimpa hasil lama.
    import os
    if os.path.exists(args.out):
        print(f"GAGAL: {args.out} sudah ada. Hapus atau pindahkan dulu kalau mau menjalankan ulang.", file=sys.stderr)
        return 1

    dst = sqlite3.connect(args.out)
    dst.executescript("""
        create table wallets (
            address           text primary key,
            first_seen_at     text not null,
            last_seen_at      text not null,
            coins_touched     integer not null,
            total_appearances integer not null,
            is_bot_suspect    integer not null default 0,
            bot_suspect_reason text
        );
        create table coins (
            mint          text primary key,
            symbol        text,
            first_seen_at text not null
        );
        create table appearances (
            wallet            text not null,
            coin_mint         text not null,
            first_seen_at     text not null,
            native_sol_volume real,
            primary key (wallet, coin_mint)
        );
    """)

    print("Membaca whale_appearances dari backup (ini bisa makan waktu beberapa detik untuk 1,2 juta baris) ...")

    # Ambil kemunculan PERTAMA per (wallet, coin_mint) — ini inti dari dedup.
    # volume_sol dari SCIA TIDAK dipercaya (99% = 10.0 tebakan), jadi kita simpan
    # sebagai referensi mentah saja (native_sol_volume), TIDAK dipakai untuk skor.
    rows = src.execute("""
        select wallet, coin_mint, coin_symbol, min(appeared_at) as first_at, volume_sol
        from whale_appearances
        group by wallet, coin_mint
    """)

    coin_first_seen = {}
    coin_symbol = {}
    wallet_stats = {}  # wallet -> {coins: set, first: str, last: str}
    appearances_to_insert = []

    row_count = 0
    for wallet, coin_mint, symbol, first_at, volume_sol in rows:
        row_count += 1
        appearances_to_insert.append((wallet, coin_mint, first_at, volume_sol))

        if coin_mint not in coin_first_seen or first_at < coin_first_seen[coin_mint]:
            coin_first_seen[coin_mint] = first_at
        if symbol and coin_mint not in coin_symbol:
            coin_symbol[coin_mint] = symbol

        st = wallet_stats.setdefault(wallet, {"coins": set(), "first": first_at, "last": first_at})
        st["coins"].add(coin_mint)
        if first_at < st["first"]:
            st["first"] = first_at
        if first_at > st["last"]:
            st["last"] = first_at

    print(f"Ditemukan {row_count} pasangan (wallet, koin) unik setelah dedup.")
    print(f"Ditemukan {len(coin_first_seen)} koin unik.")
    print(f"Ditemukan {len(wallet_stats)} wallet unik.")

    # Kita juga butuh total_appearances ASLI (sebelum dedup) untuk deteksi bot,
    # karena kriterianya "muncul 500x tapi cuma di <=3 koin" — itu perlu hitungan
    # SEBELUM dedup, bukan sesudah (sesudah dedup semua orang cuma "muncul" sekali per koin).
    print("Menghitung total kemunculan asli (sebelum dedup) per wallet untuk deteksi bot ...")
    raw_counts = src.execute("""
        select wallet, count(*) as total
        from whale_appearances
        group by wallet
    """)
    total_appearances_by_wallet = {w: c for w, c in raw_counts}

    dst.executemany(
        "insert into appearances (wallet, coin_mint, first_seen_at, native_sol_volume) values (?, ?, ?, ?)",
        appearances_to_insert,
    )
    dst.executemany(
        "insert into coins (mint, symbol, first_seen_at) values (?, ?, ?)",
        [(mint, coin_symbol.get(mint), first_at) for mint, first_at in coin_first_seen.items()],
    )

    bot_suspect_count = 0
    wallet_rows = []
    for wallet, st in wallet_stats.items():
        coins_touched = len(st["coins"])
        total_app = total_appearances_by_wallet.get(wallet, 0)

        is_bot = False
        reason = None
        if coins_touched <= LOW_COIN_COUNT and total_app >= HIGH_APPEARANCE_COUNT:
            is_bot = True
            reason = f"Muncul {total_app}x tapi hanya di {coins_touched} koin — pola sniper/bot, bukan early buyer"
            bot_suspect_count += 1

        wallet_rows.append((wallet, st["first"], st["last"], coins_touched, total_app, int(is_bot), reason))

    dst.executemany(
        """insert into wallets
           (address, first_seen_at, last_seen_at, coins_touched, total_appearances, is_bot_suspect, bot_suspect_reason)
           values (?, ?, ?, ?, ?, ?, ?)""",
        wallet_rows,
    )
    dst.commit()

    print(f"\n{bot_suspect_count} wallet ({bot_suspect_count / len(wallet_stats) * 100:.1f}%) ditandai is_bot_suspect.")
    print(f"Selesai. Ditulis ke {args.out}")
    print("\nCATATAN: apa yang BELUM dilakukan skrip ini:")
    print("  - Belum ada harga on-chain yang diambil.")
    print("  - entry_rank (urutan masuk ke koin ini) belum dihitung.")
    print("  - first_seen_at = waktu SCIA MELIHAT, bukan waktu transaksi asli di chain")
    print("    (butuh verifikasi lewat tx_signature terpisah).")
    print("  - Skor belum dihitung. Ini baru data mentah yang sudah dibersihkan.")

    src.close()
    dst.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
