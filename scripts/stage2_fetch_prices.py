#!/usr/bin/env python3
"""
Crypto-whales - Tahap 2 (v2): harga historis GeckoTerminal (publik, tanpa key).

v1 memanggil API 4x PER WALLET (koin teratas = 47.933 wallet = ~192 ribu panggilan,
tidak akan selesai). v2 memanggil API per KOIN: 1 panggilan daftar pool + 1-2 pool x
1-2 halaman candle per jam. Harga tiap wallet (entry, +1j, +4j, +24j) dicari lokal.

Aturan harga (jujur): candle 1 jam; harga di waktu T = interpolasi linear open->close
candle yang memuat T (harapan jembatan Brownian, tidak bias naik/turun). Jam tanpa
transaksi = close candle terakhir, maksimal 24 jam basi, lebih dari itu NULL. Sebelum
candle pertama = NULL (NULL = tidak diketahui, bukan 0). Hanya pool dengan token sebagai
BASE dan quote WSOL/USDC/USDT. Pool utama = reserve terbesar; kalau wallet masuk sebelum
pool utama ada (bonding curve pump.fun), pool tertua ikut. Jam sama di dua pool: volume
terbesar menang.

Keamanan: jeda --interval antar panggilan, 429 menggandakan jeda (maks 120s) dan
Retry-After dipatuhi. 404 (token tak ada) dibedakan dari error sementara; error sementara
TIDAK dicatat selesai (dicoba lagi di run berikut). Satu koin = satu transaksi database.
Berhenti sendiri setelah 8 koin gagal berturut-turut. Aman di-Ctrl+C dan dilanjut.
401/403 pada halaman candle = riwayat melewati batas 180 hari tier gratis: candle yang sudah didapat
dipakai, koin ditandai ok_partial, wallet yang masuk sebelum batas dapat harga NULL (tidak dinilai).

Pakai: python3 scripts/stage2_fetch_prices.py --stage1-db crypto_whales_stage1.db --max-coins 50
"""

import argparse
import bisect
import calendar
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SCHEMA_VERSION = 2
API_BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"
USER_AGENT = "crypto-whales/0.2 (+https://github.com/masojie/Crypto-whales)"
ACCEPT = "application/json;version=20230302"

HOUR = 3600
CANDLE_LIMIT = 1000
MAX_PAGES_PER_POOL = 8
STALE_SECONDS = 24 * HOUR
MAX_CONSECUTIVE_ERRORS = 8
HORIZONS = (("entry", 0), ("h1", 1), ("h4", 4), ("h24", 24))
MAJOR_QUOTES = {
    "So11111111111111111111111111111111111111112",   # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def log(msg):
    print(msg, flush=True)


def parse_epoch(s):
    """ISO 8601 -> epoch UTC. Aman untuk nanodetik (9 digit) ala SCIA. None kalau format asing."""
    if not isinstance(s, str):
        return None
    m = _TS_RE.match(s.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, tz = m.groups()
    epoch = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mi), int(ss), 0, 0, 0))
    if tz and tz != "Z":
        digits = tz[1:].replace(":", "")
        offset = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        epoch -= offset if tz[0] == "+" else -offset
    return epoch


class Throttle:
    """Jeda antar panggilan. 429 menggandakan jeda, sukses menurunkannya pelan."""

    def __init__(self, base_interval):
        self.base = base_interval
        self.interval = base_interval
        self.last = 0.0
        self.calls = 0
        self.rate_limited = 0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self.last = time.monotonic()
        self.calls += 1

    def penalize(self):
        self.rate_limited += 1
        self.interval = min(self.interval * 2, 120.0)

    def reward(self):
        self.interval = max(self.base, self.interval * 0.95)


def _retry_after(err):
    try:
        return float(err.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


def api_get(throttle, path, max_attempts=6):
    """-> (status, json). status: 'ok' | 'not_found' | 'forbidden' (401/403, mis. riwayat > 180 hari di tier gratis) | 'error' (sementara, BUKAN 'tidak ada')."""
    url = f"{API_BASE}{path}"
    for attempt in range(max_attempts):
        throttle.wait()
        req = urllib.request.Request(url, headers={"Accept": ACCEPT, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            throttle.reward()
            return "ok", body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "not_found", None
            if e.code in (401, 403):
                return "forbidden", None
            if e.code == 429:
                throttle.penalize()
                wait = max(_retry_after(e) or 0.0, throttle.interval)
                log(f"  429 rate limit. Jeda {wait:.0f}s (percobaan {attempt + 1}/{max_attempts})")
                time.sleep(wait)
            else:
                log(f"  HTTP {e.code} untuk {path.split('?')[0]} (percobaan {attempt + 1}/{max_attempts})")
                time.sleep(min(5 * (attempt + 1), 30))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            log(f"  gagal jaringan/parse: {type(e).__name__}: {e} (percobaan {attempt + 1}/{max_attempts})")
            time.sleep(min(5 * (attempt + 1), 30))
    return "error", None


def list_pools(throttle, mint):
    """-> (status, [(reserve_usd, created_ts|None, address)]) untuk pool yang layak."""
    status, body = api_get(throttle, f"/networks/{NETWORK}/tokens/{mint}/pools?page=1")
    if status != "ok":
        return ("error" if status == "forbidden" else status), []
    eligible = []
    for p in body.get("data") or []:
        try:
            attrs = p["attributes"]
            rel = p["relationships"]
            base_id = rel["base_token"]["data"]["id"]
            quote_id = rel["quote_token"]["data"]["id"]
            address = attrs["address"]
        except (KeyError, TypeError):
            continue
        if base_id != f"{NETWORK}_{mint}":
            continue
        if quote_id.split("_", 1)[-1] not in MAJOR_QUOTES:
            continue
        try:
            reserve = float(attrs.get("reserve_in_usd") or 0.0)
        except (TypeError, ValueError):
            reserve = 0.0
        eligible.append((reserve, parse_epoch(attrs.get("pool_created_at")), address))
    return ("ok" if eligible else "no_pool"), eligible


def select_pools(eligible, t_min):
    """Pool utama (reserve terbesar); kalau ada wallet masuk sebelum pool itu dibuat, tambah pool tertua. Maks 2."""
    top = max(eligible, key=lambda x: x[0])
    chosen = [top[2]]
    if top[1] is not None and top[1] > t_min - HOUR:
        older = [e for e in eligible if e[2] != top[2] and e[1] is not None and e[1] < top[1]]
        if older:
            chosen.append(min(older, key=lambda x: x[1])[2])
    return chosen


def parse_ohlcv(body):
    """[[ts, o, h, l, c, v], ...] -> list tuple valid. Baris rusak dibuang, tidak crash."""
    try:
        raw = body["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    rows = []
    if not isinstance(raw, list):
        return rows
    for r in raw:
        if not isinstance(r, list) or len(r) != 6:
            continue
        ts, o, _h, _l, c, v = r
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (ts, o, c)):
            continue
        rows.append((int(ts), float(o), float(c), float(v) if isinstance(v, (int, float)) else 0.0))
    return rows


def fetch_candles(throttle, pool, t_lo, t_hi):
    """Candle per jam yang menutup [t_lo, t_hi]. -> (status, {ts: (open, close, volume)}, terpotong).
    terpotong=True kalau API menolak (401/403) halaman yang lebih tua: batas 180 hari tier gratis."""
    candles = {}
    cut = False
    before = int(t_hi)
    for _ in range(MAX_PAGES_PER_POOL):
        status, body = api_get(
            throttle,
            f"/networks/{NETWORK}/pools/{pool}/ohlcv/hour"
            f"?aggregate=1&limit={CANDLE_LIMIT}&before_timestamp={before}&currency=usd",
        )
        if status == "error":
            return "error", candles, cut
        if status == "forbidden":
            cut = True
            break
        if status == "not_found":
            break
        rows = parse_ohlcv(body)
        if not rows:
            break
        for ts, o, c, v in rows:
            candles[ts] = (o, c, v)
        oldest = min(r[0] for r in rows)
        if oldest <= t_lo or len(rows) < CANDLE_LIMIT:
            break
        before = oldest
    return "ok", candles, cut


def merge_candles(series_list):
    """Gabung candle beberapa pool. Di jam yang sama, volume terbesar menang."""
    merged = {}
    for series in series_list:
        for ts, cd in series.items():
            cur = merged.get(ts)
            if cur is None or cd[2] > cur[2]:
                merged[ts] = cd
    return merged


class Series:
    def __init__(self, candles):
        self.candles = candles
        self.ts = sorted(candles)

    def price_at(self, t):
        """Harga USD pada detik epoch t, atau None kalau tidak diketahui."""
        h = t - t % HOUR
        cd = self.candles.get(h)
        if cd is not None:
            o, c, _ = cd
            p = o + (c - o) * ((t - h) / HOUR)
            return p if p > 0 else None
        i = bisect.bisect_left(self.ts, h) - 1
        if i < 0:
            return None
        last = self.ts[i]
        if h - (last + HOUR) > STALE_SECONDS:
            return None
        p = self.candles[last][1]
        return p if p > 0 else None


SCHEMA = """
create table if not exists price_points (
    wallet     text not null,
    coin_mint  text not null,
    horizon    text not null check (horizon in ('entry','h1','h4','h24')),
    price_usd  real,
    fetched_at text not null,
    primary key (wallet, coin_mint, horizon)
) without rowid;
create table if not exists coin_stats (
    coin_mint  text primary key,
    status     text not null,
    n_wallets  integer not null,
    pools      text,
    n_candles  integer,
    calls      integer,
    entry_ok   integer,
    h1_ok      integer,
    h4_ok      integer,
    h24_ok     integer,
    done_at    text not null
);
"""


def open_out(path):
    """Buka/buat database output. None kalau file itu peninggalan stage2 v1."""
    existed = os.path.exists(path)
    db = sqlite3.connect(path)
    version = db.execute("pragma user_version").fetchone()[0]
    if existed and version != SCHEMA_VERSION:
        has_tables = db.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
        if has_tables:
            db.close()
            return None
    db.executescript(SCHEMA)
    db.execute(f"pragma user_version = {SCHEMA_VERSION}")
    db.commit()
    return db


def process_coin(out, stage1, throttle, mint, now_ts):
    """-> (status, info). status: ok | not_found | no_pool | no_candles | no_entries | error."""
    calls_before = throttle.calls
    entries = []
    for wallet, first_seen in stage1.execute("select wallet, first_seen_at from appearances where coin_mint=?", (mint,)):
        e = parse_epoch(first_seen)
        if e is not None:
            entries.append((wallet, e))
    n_wallets = len(entries)
    done_at = datetime.now(timezone.utc).isoformat()

    def finish(status, pools="", n_candles=0, rows=None, ok=(0, 0, 0, 0)):
        with out:
            if rows:
                out.executemany(
                    "insert or replace into price_points (wallet, coin_mint, horizon, price_usd, fetched_at) values (?,?,?,?,?)",
                    rows,
                )
            out.execute(
                "insert or replace into coin_stats values (?,?,?,?,?,?,?,?,?,?,?)",
                (mint, status, n_wallets, pools, n_candles, throttle.calls - calls_before, *ok, done_at),
            )
        return status, {"n_wallets": n_wallets, "n_candles": n_candles, "calls": throttle.calls - calls_before, "ok": ok, "pools": pools}

    if not entries:
        return finish("no_entries")

    t_min = min(e for _, e in entries)
    t_max = max(e for _, e in entries)

    status, eligible = list_pools(throttle, mint)
    if status == "error":
        return "error", {}
    if status in ("not_found", "no_pool"):
        return finish(status)

    pools = select_pools(eligible, t_min)
    t_lo = t_min - STALE_SECONDS - HOUR
    t_hi = t_max + 25 * HOUR
    all_series = []
    any_cut = False
    for pool in pools:
        st, candles, cut = fetch_candles(throttle, pool, t_lo, t_hi)
        if st == "error":
            return "error", {}
        any_cut = any_cut or cut
        all_series.append(candles)
    merged = merge_candles(all_series)
    if not merged:
        return finish("no_history" if any_cut else "no_candles", ",".join(pools))

    series = Series(merged)
    rows = []
    counts = [0, 0, 0, 0]
    for wallet, e in entries:
        for k, (label, hours) in enumerate(HORIZONS):
            t = e + hours * HOUR
            p = series.price_at(t) if t <= now_ts else None
            if p is not None:
                counts[k] += 1
            rows.append((wallet, mint, label, p, done_at))
    return finish("ok_partial" if any_cut else "ok", ",".join(pools), len(merged), rows, tuple(counts))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ambil harga historis GeckoTerminal untuk koin di stage1 (per koin, dengan checkpoint).")
    ap.add_argument("--stage1-db", required=True, help="crypto_whales_stage1.db dari tahap 1")
    ap.add_argument("--out", default="crypto_whales_stage2.db", help="database output (dilanjutkan kalau sudah ada)")
    ap.add_argument("--max-coins", type=int, default=None, help="batasi jumlah koin sesi ini")
    ap.add_argument("--interval", type=float, default=6.5, help="jeda minimal antar panggilan API, detik (default 6.5)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.stage1_db):
        print(f"GAGAL: {args.stage1_db} tidak ada.", file=sys.stderr)
        return 1
    stage1 = sqlite3.connect(f"file:{args.stage1_db}?mode=ro", uri=True)
    stage1.execute("pragma query_only = ON")

    out = open_out(args.out)
    if out is None:
        print(f"GAGAL: {args.out} peninggalan stage2 versi lama (v1) dan tidak kompatibel.", file=sys.stderr)
        print(f"Hapus dulu: rm {args.out}", file=sys.stderr)
        return 1

    order = stage1.execute("select coin_mint, count(*) c from appearances group by coin_mint order by c desc, coin_mint").fetchall()
    done = {r[0] for r in out.execute("select coin_mint from coin_stats")}
    remaining = [(m, c) for m, c in order if m not in done]
    log(f"Total {len(order)} koin. Selesai sebelumnya: {len(done)}. Sisa: {len(remaining)}.")
    if args.max_coins is not None:
        remaining = remaining[: args.max_coins]
        log(f"Dibatasi --max-coins={args.max_coins}: {len(remaining)} koin sesi ini.")
    if not remaining:
        log("Tidak ada koin tersisa.")
        return 0

    throttle = Throttle(args.interval)
    now_ts = int(time.time())
    started = time.monotonic()
    processed = 0
    consecutive_errors = 0
    exit_code = 0
    try:
        for i, (mint, n) in enumerate(remaining, 1):
            status, info = process_coin(out, stage1, throttle, mint, now_ts)
            if status == "error":
                consecutive_errors += 1
                log(f"[{i}/{len(remaining)}] {mint[:10]}... ({n} wallet) GAGAL sementara, dicoba lagi di run berikut ({consecutive_errors} berturut-turut)")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log(f"BERHENTI: {MAX_CONSECUTIVE_ERRORS} koin gagal berturut-turut. API menolak terus. Coba lagi nanti atau ganti jaringan.")
                    exit_code = 2
                    break
                continue
            consecutive_errors = 0
            processed += 1
            eta_h = (time.monotonic() - started) / i * (len(remaining) - i) / 3600
            if status in ("ok", "ok_partial"):
                ok = info["ok"]
                pct = lambda k: 100.0 * ok[k] / max(info["n_wallets"], 1)
                log(f"[{i}/{len(remaining)}] {mint[:10]}... w={info['n_wallets']} candle={info['n_candles']} panggilan={info['calls']} "
                    f"entry={pct(0):.0f}% h4={pct(2):.0f}% (sisa ~{eta_h:.1f} jam, jeda {throttle.interval:.1f}s)"
                    + (" [RIWAYAT TERPOTONG 180 HARI]" if status == "ok_partial" else ""))
            else:
                log(f"[{i}/{len(remaining)}] {mint[:10]}... w={info['n_wallets']} status={status} (tanpa harga)")
    except KeyboardInterrupt:
        log("\nDihentikan (Ctrl+C). Progres sampai koin terakhir yang selesai sudah tersimpan.")

    total_done = len(done) + processed
    log(f"\nSesi ini: {processed} koin selesai. Total: {total_done}/{len(order)} koin. 429 diterima: {throttle.rate_limited}.")
    if total_done < len(order):
        log("Masih ada koin tersisa. Jalankan perintah yang sama untuk melanjutkan.")
    stage1.close()
    out.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
