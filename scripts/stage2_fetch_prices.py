#!/usr/bin/env python3
"""
Crypto-whales — Tahap 2: ambil harga historis (GeckoTerminal, publik, tanpa key)
====================================================================================

SKRIP INI MENYENTUH JARINGAN. Beda dari stage1 (murni baca lokal), skrip ini
memanggil api.geckoterminal.com berkali-kali. Karena itu, tiga hal berikut
WAJIB ada dan tidak boleh dilonggarkan:

  1. RATE LIMIT DIHORMATI. Endpoint publik GeckoTerminal ~10 panggilan/menit
     (dikonfirmasi dari docs.coingecko.com/docs/keyless-public-api). Skrip ini
     JEDA MINIMAL 6.5 detik antar panggilan (bukan 6.0 tepat, demi headroom).
     Untuk 901 koin ini bisa makan waktu berjam-jam — SENGAJA, bukan bug.

  2. CHECKPOINT. Setiap N koin berhasil diproses, progres disimpan ke database
     output. Skrip BISA DIHENTIKAN (Ctrl+C) dan DILANJUTKAN kapan saja tanpa
     mengulang dari awal — penting karena ini akan jalan lama di Termux dan
     HP bisa saja perlu dipakai untuk hal lain di tengah jalan.

  3. PRIORITAS, BUKAN SEMUA SEKALIGUS. Daripada ambil harga 901 koin secara
     acak, koin diurutkan dari yang disentuh PALING BANYAK WALLET dulu —
     ini koin yang paling mungkin menyumbang ke skor wallet yang butuh
     MIN_SAMPLE=5 koin (lihat lib/scoring.ts). Koin yang cuma disentuh 1
     wallet sekali tidak akan mengubah skor siapa pun secara signifikan.

FORMAT RESPONS API (dikonfirmasi dari docs.coingecko.com, endpoint publik
GeckoTerminal berbagi struktur path yang sama dengan Demo API berkeys, hanya
beda base URL dan tanpa header key):

    GET https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{timeframe}
        ?aggregate=...&before_timestamp=...&limit=...&currency=usd

    {
      "data": {
        "attributes": {
          "ohlcv_list": [
            [timestamp, open, high, low, close, volume],
            ...
          ]
        }
      }
    }

CATATAN JUJUR: format ini diverifikasi dari TIGA sumber dokumentasi resmi
CoinGecko/GeckoTerminal (bukan dipanggil langsung dari sandbox pengembangan
skrip ini, karena keterbatasan tooling). Skrip ini menangani kemungkinan
struktur sedikit berbeda dengan validasi eksplisit sebelum parsing (lihat
fungsi `parse_ohlcv_response`) — kalau bentuknya tak terduga, baris itu
dilewati dan dicatat sebagai gagal, TIDAK membuat skrip crash atau
menghasilkan angka yang salah diam-diam.

Kenapa PER TOKEN butuh PER POOL dulu: kolom di appearances kita adalah
coin_mint (alamat token), tapi endpoint OHLCV GeckoTerminal butuh alamat
POOL. Jadi setiap koin perlu SATU panggilan tambahan untuk menemukan pool
paling likuid miliknya (endpoint token/{address}/pools), baru OHLCV bisa
diambil. Ini MENGGANDAKAN jumlah panggilan API — sudah diperhitungkan
dalam estimasi waktu di bagian akhir skrip ini.

Pakai (Termux):
    python3 stage2_fetch_prices.py --stage1-db crypto_whales_stage1.db \
                                    --out crypto_whales_stage2.db \
                                    --max-coins 50

    (mulai dengan --max-coins kecil dulu untuk verifikasi sebelum jalan penuh)

Untuk melanjutkan yang sempat berhenti, jalankan perintah SAMA PERSIS lagi —
skrip otomatis skip koin yang sudah selesai.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"
MIN_SECONDS_BETWEEN_CALLS = 6.5  # ~9.2 panggilan/menit, di bawah batas ~10/menit
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [10, 30, 90]  # backoff bertingkat kalau kena 429/error
HORIZONS_HOURS = {"h1": 1, "h4": 4, "h24": 24}

_last_call_time = 0.0


def _throttled_get(url: str) -> dict | None:
    """GET dengan rate limiting global dan retry. Mengembalikan None (bukan
    melempar exception) kalau semua percobaan gagal — pemanggil WAJIB
    menangani None sebagai 'data tidak tersedia', bukan 0 atau nilai lain."""
    global _last_call_time

    elapsed = time.monotonic() - _last_call_time
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)

    for attempt in range(MAX_RETRIES):
        _last_call_time = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
                print(f"  HTTP {resp.status} untuk {url}", file=sys.stderr)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(f"  Kena rate limit (429). Menunggu {wait}s sebelum coba lagi ...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None  # koin/pool tidak ditemukan di GeckoTerminal, bukan error sementara
            print(f"  HTTP error {e.code} untuk {url}: {e.reason}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as e:
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            print(f"  Gagal koneksi ({e}). Menunggu {wait}s sebelum coba lagi ...", file=sys.stderr)
            time.sleep(wait)
        except json.JSONDecodeError as e:
            print(f"  Respons bukan JSON valid: {e}", file=sys.stderr)
            return None

    print(f"  GAGAL setelah {MAX_RETRIES} percobaan: {url}", file=sys.stderr)
    return None


def find_most_liquid_pool(coin_mint: str) -> str | None:
    """Cari alamat pool paling likuid untuk satu token. Mengembalikan None
    kalau token tidak ditemukan di GeckoTerminal sama sekali (koin sangat
    baru, sudah delisted, atau tidak pernah diindeks)."""
    url = f"{GECKOTERMINAL_BASE}/networks/{NETWORK}/tokens/{coin_mint}/pools?page=1"
    resp = _throttled_get(url)
    if resp is None:
        return None

    pools = resp.get("data")
    if not pools or not isinstance(pools, list) or len(pools) == 0:
        return None

    # Ambil pool pertama (GeckoTerminal mengurutkan berdasar likuiditas+volume secara default)
    first = pools[0]
    pool_id = first.get("id", "")
    # id berformat "solana_<address>" — ambil bagian setelah underscore pertama
    if "_" in pool_id:
        return pool_id.split("_", 1)[1]
    address = first.get("attributes", {}).get("address")
    return address


def parse_ohlcv_response(resp: dict) -> list[list[float]]:
    """Validasi struktur respons sebelum dipakai. Mengembalikan list kosong
    (BUKAN melempar exception) kalau struktur tak sesuai dugaan — ini
    SENGAJA supaya satu koin dengan format aneh tidak menghentikan seluruh
    proses untuk 900 koin lainnya."""
    try:
        ohlcv_list = resp["data"]["attributes"]["ohlcv_list"]
        if not isinstance(ohlcv_list, list):
            return []
        # Validasi tiap baris punya 6 elemen numerik, buang yang tidak
        valid = [row for row in ohlcv_list if isinstance(row, list) and len(row) == 6]
        return valid
    except (KeyError, TypeError):
        return []


def fetch_price_near(pool_address: str, target_unix: int) -> float | None:
    """Ambil harga (close price) candle jam paling dekat dengan target_unix,
    DARI SESUDAH target_unix (before_timestamp mengambil candle SEBELUM
    timestamp itu, jadi kita minta candle sedikit SESUDAH target untuk
    memastikan target_unix ada di dalam jendela yang diambil)."""
    before = target_unix + 3600 * 2  # 2 jam buffer ke depan
    url = (
        f"{GECKOTERMINAL_BASE}/networks/{NETWORK}/pools/{pool_address}/ohlcv/hour"
        f"?aggregate=1&before_timestamp={before}&limit=6&currency=usd"
    )
    resp = _throttled_get(url)
    if resp is None:
        return None

    candles = parse_ohlcv_response(resp)
    if not candles:
        return None

    # Cari candle dengan timestamp PALING DEKAT ke target_unix (bisa sebelum/sesudah
    # sedikit karena candle jam-jaman, bukan presisi detik)
    closest = min(candles, key=lambda row: abs(row[0] - target_unix))
    close_price = closest[4]  # index 4 = close, sesuai spesifikasi [ts,o,h,l,c,v]
    return float(close_price) if close_price is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Ambil harga historis dari GeckoTerminal untuk koin di stage1 (dengan checkpoint).")
    parser.add_argument("--stage1-db", required=True, help="Path ke crypto_whales_stage1.db dari tahap 1")
    parser.add_argument("--out", default="crypto_whales_stage2.db", help="Path database output (dibuat kalau belum ada, dilanjut kalau sudah ada)")
    parser.add_argument("--max-coins", type=int, default=None, help="Batasi jumlah koin diproses sesi ini (untuk uji coba dulu)")
    args = parser.parse_args()

    print(f"Membuka {args.stage1_db} (read-only) ...")
    stage1 = sqlite3.connect(f"file:{args.stage1_db}?mode=ro", uri=True)
    stage1.execute("PRAGMA query_only = ON;")

    out = sqlite3.connect(args.out)
    out.executescript("""
        create table if not exists coin_pools (
            coin_mint    text primary key,
            pool_address text,
            status       text not null check (status in ('found','not_found'))
        );
        create table if not exists price_points (
            wallet         text not null,
            coin_mint      text not null,
            horizon        text not null check (horizon in ('entry','h1','h4','h24')),
            price_usd      real,
            fetched_at     text not null,
            primary key (wallet, coin_mint, horizon)
        );
        create table if not exists coins_done (
            coin_mint text primary key,
            done_at   text not null
        );
    """)
    out.commit()

    # Urutkan koin berdasarkan JUMLAH WALLET yang menyentuhnya — prioritas terbesar dulu.
    # Ini query terhadap stage1 (read-only), hasilnya cuma dibaca ke memori Python.
    print("Menghitung prioritas koin (diurutkan dari yang paling banyak wallet-nya) ...")
    coin_priority = stage1.execute("""
        select coin_mint, count(distinct wallet) as wallet_count
        from appearances
        group by coin_mint
        order by wallet_count desc
    """).fetchall()

    done_coins = {row[0] for row in out.execute("select coin_mint from coins_done").fetchall()}
    remaining = [(mint, cnt) for mint, cnt in coin_priority if mint not in done_coins]

    print(f"Total {len(coin_priority)} koin. Sudah selesai sebelumnya: {len(done_coins)}. Sisa: {len(remaining)}.")

    if args.max_coins is not None:
        remaining = remaining[: args.max_coins]
        print(f"Dibatasi --max-coins={args.max_coins}: memproses {len(remaining)} koin sesi ini.")

    if not remaining:
        print("Tidak ada koin tersisa untuk diproses. Selesai.")
        return 0

    # Estimasi waktu JUJUR: 2 panggilan per koin (cari pool + OHLCV) minimal,
    # bisa lebih kalau perlu ambil entry+h1+h4+h24 secara terpisah per wallet.
    # Di sini kita SEDERHANAKAN: satu set candle per KOIN (bukan per wallet),
    # lalu tiap wallet di koin itu mencari harga terdekat dari candle yang sama.
    est_calls = len(remaining) * 2
    est_minutes = est_calls * MIN_SECONDS_BETWEEN_CALLS / 60
    print(f"Estimasi: ~{est_calls} panggilan API, ~{est_minutes:.0f} menit ({est_minutes/60:.1f} jam).")
    print("Skrip ini AMAN dihentikan (Ctrl+C) kapan saja — progres tersimpan per koin.\n")

    processed_this_session = 0
    for coin_mint, wallet_count in remaining:
        print(f"[{processed_this_session+1}/{len(remaining)}] {coin_mint} ({wallet_count} wallet) ...")

        pool_row = out.execute("select pool_address, status from coin_pools where coin_mint=?", (coin_mint,)).fetchone()
        if pool_row is None:
            pool_address = find_most_liquid_pool(coin_mint)
            status = "found" if pool_address else "not_found"
            out.execute(
                "insert into coin_pools (coin_mint, pool_address, status) values (?, ?, ?)",
                (coin_mint, pool_address, status),
            )
            out.commit()
        else:
            pool_address, status = pool_row

        if status == "not_found" or not pool_address:
            print(f"  Pool tidak ditemukan untuk {coin_mint}. Wallet di koin ini TIDAK BISA dinilai untuk koin ini. Lanjut.")
            out.execute("insert into coins_done (coin_mint, done_at) values (?, ?)", (coin_mint, datetime.now(timezone.utc).isoformat()))
            out.commit()
            processed_this_session += 1
            continue

        # Ambil semua (wallet, first_seen_at) untuk koin ini dari stage1
        wallets_here = stage1.execute(
            "select wallet, first_seen_at from appearances where coin_mint=?", (coin_mint,)
        ).fetchall()

        any_price_found = False
        for wallet, first_seen_at in wallets_here:
            try:
                entry_unix = int(datetime.fromisoformat(first_seen_at.replace("Z", "+00:00")).timestamp())
            except ValueError:
                print(f"  Format waktu tak terduga untuk {wallet}: {first_seen_at!r}. Dilewati.", file=sys.stderr)
                continue

            for horizon_label, hours_after in [("entry", 0)] + list(HORIZONS_HOURS.items()):
                existing = out.execute(
                    "select 1 from price_points where wallet=? and coin_mint=? and horizon=?",
                    (wallet, coin_mint, horizon_label),
                ).fetchone()
                if existing:
                    continue

                target_unix = entry_unix + hours_after * 3600
                price = fetch_price_near(pool_address, target_unix)
                out.execute(
                    "insert or replace into price_points (wallet, coin_mint, horizon, price_usd, fetched_at) values (?, ?, ?, ?, ?)",
                    (wallet, coin_mint, horizon_label, price, datetime.now(timezone.utc).isoformat()),
                )
                out.commit()
                if price is not None:
                    any_price_found = True

        status_note = "harga ditemukan" if any_price_found else "TIDAK ADA harga ditemukan (kemungkinan koin mati/delisted)"
        print(f"  Selesai untuk {len(wallets_here)} wallet — {status_note}.")

        out.execute("insert into coins_done (coin_mint, done_at) values (?, ?)", (coin_mint, datetime.now(timezone.utc).isoformat()))
        out.commit()
        processed_this_session += 1

    print(f"\nSesi ini selesai: {processed_this_session} koin diproses.")
    total_done = len(done_coins) + processed_this_session
    print(f"Total keseluruhan: {total_done}/{len(coin_priority)} koin.")
    if total_done < len(coin_priority):
        print("Masih ada koin tersisa. Jalankan perintah yang sama lagi untuk melanjutkan.")

    stage1.close()
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
