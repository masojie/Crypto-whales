#!/usr/bin/env python3
"""
Crypto-whales — Tahap 2 (on-chain): harga entry per transaksi + harga horizon dari chain.

Pengganti stage2_fetch_prices.py untuk data dari extract_onchain.py. Keluaran memakai
skema price_points SAMA dengan stage2 lama, jadi stage3 jalan tanpa diubah.

Kenapa dibuat:
  stage2 lama memakai candle per jam GeckoTerminal. Semua early buyer satu koin masuk di
  jam yang sama, sehingga semuanya mendapat harga entry identik dan skor tidak bisa
  membedakan wallet. Untuk token pump.fun, pool baru ada setelah migrasi sehingga candle
  untuk masa bonding curve juga tidak ada.

Cara kerja:
  - entry  = harga fill transaksi beli wallet itu sendiri (SOL/token), dari extract_onchain.py
  - h1/h4/h24 = median harga trade koin pada jendela waktu setelah entry wallet
                (dibaca dari transaksi koin di chain via Helius, curve maupun AMM)
  - Bila tidak ada trade di jendela, dipakai harga trade TERAKHIR sebelum horizon (koin mati dinilai
    dengan harga terakhirnya, bukan dibuang). Matikan dengan --no-carry.
  - Horizon yang belum lewat, atau koin yang tidak punya trade sama sekali = NULL, tidak ditebak.

SATUAN: kolom price_usd berisi harga dalam SOL per token (tabel meta mencatatnya).
Stage3 hanya memakai rasio entry vs h4, jadi selama satuannya konsisten hasilnya valid.

Kunci API dari env HELIUS_API_KEY.
"""
import argparse, os, sqlite3, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_onchain as x

HORIZONS = {"h1": 3600, "h4": 4 * 3600, "h24": 24 * 3600}


def compute_coin(mint, rows, now, carry, horizons):
    """Semua titik harga satu koin. Dipanggil di thread terpisah (hanya I/O jaringan)."""
    cache, got, carried, out = {}, {h: 0 for h in horizons}, 0, []
    for wallet, ets, eprice in rows:
        pts = {"entry": eprice}
        for h in horizons:
            target = ets + HORIZONS[h]
            if target > now - 900:      # horizon belum lewat (+15 menit buffer)
                pts[h] = None
                continue
            key = (h, target // 60)     # wallet yang masuk dalam menit yang sama berbagi jendela
            if key not in cache:
                pr, _, kind = x.window_price(mint, (target // 60) * 60, carry=carry)
                cache[key] = (pr, kind)
            pts[h], kind = cache[key]
            got[h] += pts[h] is not None
            carried += kind == "carry"
        out.append((wallet, pts))
    return out, got, carried


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    p = argparse.ArgumentParser(description="Harga entry per transaksi + harga horizon dari chain")
    p.add_argument("--stage1-db", required=True, help="DB keluaran extract_onchain.py")
    p.add_argument("--out", default="stage2_onchain.db")
    p.add_argument("--max-coins", type=int, default=0, help="0 = semua")
    p.add_argument("--workers", type=int, default=4, help="jumlah koin yang diproses paralel")
    p.add_argument("--horizons", default="h1,h4,h24", help="mis. 'h4' saja: stage3 hanya memakai entry dan h4, jadi lebih cepat 3x")
    p.add_argument("--no-carry", action="store_true", help="jangan pakai harga trade terakhir untuk koin tanpa trade di jendela (hasil bias ke koin hidup)")
    a = p.parse_args()
    x.helius_key()
    horizons = [h.strip() for h in a.horizons.split(",") if h.strip() in HORIZONS]
    if not horizons:
        raise SystemExit("--horizons harus berisi h1, h4, dan/atau h24")

    s1 = sqlite3.connect(f"file:{a.stage1_db}?mode=ro", uri=True)
    out = sqlite3.connect(a.out)
    out.executescript("""
        CREATE TABLE IF NOT EXISTS price_points(wallet TEXT NOT NULL, coin_mint TEXT NOT NULL,
            horizon TEXT NOT NULL CHECK (horizon IN ('entry','h1','h4','h24')), price_usd REAL,
            fetched_at TEXT NOT NULL, PRIMARY KEY(wallet, coin_mint, horizon));
        CREATE TABLE IF NOT EXISTS coins_done(coin_mint TEXT PRIMARY KEY, done_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT OR REPLACE INTO meta VALUES('price_unit','SOL per token (kolom price_usd berisi SOL, bukan USD)');
    """)
    done = {r[0] for r in out.execute("SELECT coin_mint FROM coins_done")}
    coins = [r[0] for r in s1.execute(
        "SELECT coin_mint FROM appearance_entry GROUP BY coin_mint ORDER BY COUNT(*) DESC") if r[0] not in done]
    if a.max_coins:
        coins = coins[: a.max_coins]
    print(f"{len(coins)} koin diproses (sudah selesai sebelumnya: {len(done)}), horizon {horizons}, {a.workers} worker.", flush=True)

    now = int(time.time())
    jobs = {c: s1.execute("SELECT wallet, entry_ts, entry_price_sol FROM appearance_entry WHERE coin_mint=?", (c,)).fetchall() for c in coins}
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(compute_coin, m, rows, now, not a.no_carry, horizons): m for m, rows in jobs.items()}
        for i, fu in enumerate(as_completed(futs), 1):
            mint = futs[fu]
            try:
                pts_all, got, carried = fu.result()
            except Exception as e:
                print(f"[{i}/{len(coins)}] {mint[:12]}.. GAGAL: {type(e).__name__}", file=sys.stderr)
                continue
            stamp = datetime.now(timezone.utc).isoformat()
            for wallet, pts in pts_all:
                for h, v in pts.items():
                    out.execute("INSERT OR REPLACE INTO price_points VALUES(?,?,?,?,?)", (wallet, mint, h, v, stamp))
            out.execute("INSERT OR REPLACE INTO coins_done VALUES(?,?)", (mint, stamp))
            out.commit()
            print(f"[{i}/{len(coins)}] {mint[:12]}.. {len(pts_all)} wallet | " + " ".join(f"{h}={n}" for h, n in got.items()) + f" (carry {carried})", flush=True)
    print(f"\nSelesai. Satuan harga: SOL per token. Keluaran: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
