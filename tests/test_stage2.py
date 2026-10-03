#!/usr/bin/env python3
"""
Uji stage2 v2 dengan server GeckoTerminal TIRUAN (tanpa internet). Jalankan: python3 tests/test_stage2.py
Harga tiruan linear: P(t) = 1 + 0.01 * (jam sejak BASE). Interpolasi stage2 juga linear di dalam
candle, jadi harga benar untuk waktu apa pun DIKETAHUI PASTI dan dibandingkan dengan hasil stage2.
"""
import contextlib
import http.server
import io
import json
import os
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import stage2_fetch_prices as s2

H = 3600
BASE = 1_780_000_000 - (1_780_000_000 % H)
WSOL = "So11111111111111111111111111111111111111112"
MEME = "SomeMemeCoin1111111111111111111111111111111111"
WORLD, POOLS, FAIL, CALLS, FAILS = {}, {}, {}, [], []


def P(t):
    return 1.0 + 0.01 * (t - BASE) / H


def at(hours):
    return BASE + int(round(hours * H))


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def candles(h_from, h_to, vol, scale=1.0, skip=()):
    out = {}
    for k in range(h_from, h_to):
        if k not in skip:
            ts = BASE + k * H
            out[ts] = (P(ts) * scale, P(ts + H) * scale, vol)
    return out


def add_pool(mint, addr, reserve, created_h, cand, base=None, quote=WSOL):
    WORLD.setdefault(mint, []).append(dict(addr=addr, reserve=reserve, base=base or mint, quote=quote,
                                           created=None if created_h is None else BASE + int(created_h * H)))
    POOLS[addr] = cand


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _injected(key):
        seq = FAIL.get(key)
        if seq == "always":
            return 500
        if isinstance(seq, list) and seq:
            return seq.pop(0)
        return None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        parts = u.path.strip("/").split("/")
        CALLS.append(u.path)
        if len(parts) == 7 and parts[4] == "tokens" and parts[6] == "pools":
            mint = parts[5]
            code = self._injected(("pools", mint))
            if code:
                return self._send(code, {"errors": []}, {"Retry-After": "0"} if code == 429 else None)
            if mint not in WORLD:
                return self._send(404, {"errors": [{"status": "404"}]})
            data = []
            for p in WORLD[mint]:
                attrs = {"address": p["addr"], "name": "X / Y", "reserve_in_usd": str(p["reserve"])}
                if p["created"] is not None:
                    attrs["pool_created_at"] = iso(p["created"])
                data.append({"id": "solana_" + p["addr"], "type": "pool", "attributes": attrs, "relationships": {
                    "base_token": {"data": {"id": "solana_" + p["base"], "type": "token"}},
                    "quote_token": {"data": {"id": "solana_" + p["quote"], "type": "token"}},
                    "dex": {"data": {"id": "dex", "type": "dex"}}}})
            return self._send(200, {"data": data})
        if len(parts) == 8 and parts[4] == "pools" and parts[6] == "ohlcv":
            addr = parts[5]
            code = self._injected(("ohlcv", addr))
            if code:
                return self._send(code, {"errors": []}, {"Retry-After": "0"} if code == 429 else None)
            if addr not in POOLS:
                return self._send(404, {"errors": [{"status": "404"}]})
            before, limit = int(q["before_timestamp"][0]), int(q["limit"][0])
            rows = sorted(((ts, cd[0], cd[1], cd[2]) for ts, cd in POOLS[addr].items() if ts < before), reverse=True)[:limit]
            return self._send(200, {"data": {"attributes": {"ohlcv_list": [[ts, o, max(o, c), min(o, c), c, v] for ts, o, c, v in rows]}}})
        self._send(404, {"errors": ["rute tak dikenal"]})


def check(cond, label):
    print(("lolos  " if cond else "GAGAL  ") + label)
    if not cond:
        FAILS.append(label)


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def check_price(out, wallet, mint, horizon, expected, label):
    r = out.execute("select price_usd from price_points where wallet=? and coin_mint=? and horizon=?", (wallet, mint, horizon)).fetchone()
    got = "MISSING" if r is None else r[0]
    ok = (got is None and expected is None) or (expected is not None and got not in (None, "MISSING") and near(got, expected))
    check(ok, f"{label} {wallet} {horizon}: dapat={got} harapan={expected}")


def make_stage1(path, rows):
    """rows: (wallet, mint, jam). Waktu ditulis dengan nanodetik seperti SCIA."""
    db = sqlite3.connect(path)
    db.executescript("""
        create table appearances(wallet text, coin_mint text, first_seen_at text, native_sol_volume real, primary key(wallet, coin_mint));
        create table wallets(address text primary key, first_seen_at text, last_seen_at text, coins_touched integer,
                             total_appearances integer, is_bot_suspect integer, bot_suspect_reason text);""")
    db.executemany("insert into appearances values (?,?,?,NULL)", [(w, m, iso(at(h)).replace("Z", ".987654321+00:00")) for w, m, h in rows])
    for w in {r[0] for r in rows}:
        db.execute("insert into wallets values (?,?,?,?,?,0,NULL)", (w, "x", "x", 1, 1))
    db.commit()
    db.close()


def quiet_main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = s2.main(argv)
    return code, buf.getvalue()


def dump_prices(path):
    db = sqlite3.connect(path)
    rows = db.execute("select wallet, coin_mint, horizon, price_usd from price_points order by 1,2,3").fetchall()
    db.close()
    return rows


def main():
    s2.time.sleep = lambda s: None
    s2.CANDLE_LIMIT = 50
    tmp = tempfile.mkdtemp()
    add_pool("MintA", "PA", 1e6, -100, candles(0, 400, 100))
    add_pool("MintB", "PBmain", 1e6, 100, candles(100, 301, 1000))
    add_pool("MintB", "PBcurve", 10, 0, {**candles(0, 100, 10), **candles(100, 111, 1, scale=2.0)})
    add_pool("MintC", "PC", 1e6, 0, candles(0, 200, 100, skip=set(range(50, 60))))
    add_pool("MintE", "PE1", 1e6, 0, candles(0, 50, 100), base="OtherToken")
    add_pool("MintE", "PE2", 1e6, 0, candles(0, 50, 100), quote=MEME)
    add_pool("MintF", "PF", 1e6, 0, candles(0, 100, 100))
    add_pool("MintH", "PH", 1e6, 0, candles(0, 100, 100))
    FAIL[("pools", "MintF")] = [500]
    FAIL[("ohlcv", "PF")] = [429, 500]
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s2.API_BASE = f"http://127.0.0.1:{srv.server_address[1]}/api/v2"

    entries = [("wA1", "MintA", 10.5), ("wA2", "MintA", 120.25), ("wA3", "MintA", 300.75),
               ("wB1", "MintB", 20.5), ("wB2", "MintB", 90.5), ("wB3", "MintB", 105.5), ("wB4", "MintB", 150.25),
               ("wC0", "MintC", -5.0), ("wC1", "MintC", 55.5), ("wC2", "MintC", 210.0), ("wC3", "MintC", 400.0),
               ("wD1", "MintD", 10.0), ("wE1", "MintE", 10.0), ("wF1", "MintF", 10.5), ("wH1", "MintH", 10.5)]
    s1p = os.path.join(tmp, "s1.db")
    make_stage1(s1p, entries)
    stage1 = sqlite3.connect(s1p)
    out = s2.open_out(os.path.join(tmp, "s2.db"))
    now_ts = at(5000)
    run = lambda mint, th=None, now=now_ts: s2.process_coin(out, stage1, th or s2.Throttle(0.0), mint, now)

    print("== parse_epoch")
    check(s2.parse_epoch("2026-04-05T20:00:09.023280351+00:00") == 1775419209, "nanodetik +00:00")
    check(s2.parse_epoch("2026-04-05T20:00:09Z") == 1775419209, "akhiran Z")
    check(s2.parse_epoch("2026-04-06T03:00:09+07:00") == 1775419209, "offset +07:00")
    check(s2.parse_epoch("bukan waktu") is None and s2.parse_epoch(None) is None, "format asing = None")

    print("== MintA: interpolasi + paging (limit 50 candle per panggilan)")
    n0 = len(CALLS)
    st, _ = run("MintA")
    check(st == "ok", "MintA status ok")
    check(sum(1 for c in CALLS[n0:] if "/pools/PA/ohlcv/" in c) == 7, "MintA butuh tepat 7 halaman candle")
    for w, _, h in [e for e in entries if e[1] == "MintA"]:
        for label, k in (("entry", 0), ("h1", 1), ("h4", 4), ("h24", 24)):
            check_price(out, w, "MintA", label, P(at(h + k)), "MintA")

    print("== MintB: bonding curve (pool tertua) + volume terbesar menang")
    n0 = len(CALLS)
    st, _ = run("MintB")
    check(st == "ok", "MintB status ok")
    check(any("/pools/PBcurve/ohlcv/" in c for c in CALLS[n0:]) and any("/pools/PBmain/ohlcv/" in c for c in CALLS[n0:]), "MintB mengambil KEDUA pool")
    for w, _, h in [e for e in entries if e[1] == "MintB"]:
        for label, k in (("entry", 0), ("h1", 1), ("h4", 4), ("h24", 24)):
            check_price(out, w, "MintB", label, P(at(h + k)), "MintB (jam tumpang tindih ikut pool volume besar)")

    print("== MintC: jam kosong, sebelum candle pertama, terlalu basi")
    st, _ = run("MintC")
    check(st == "ok", "MintC status ok")
    for label in ("entry", "h1", "h4"):
        check_price(out, "wC0", "MintC", label, None, "sebelum candle pertama = NULL")
    check_price(out, "wC0", "MintC", "h24", P(at(19)), "wC0 masuk candle")
    for label in ("entry", "h1", "h4"):
        check_price(out, "wC1", "MintC", label, P(at(50)), "jam kosong -> close candle terakhir")
    check_price(out, "wC1", "MintC", "h24", P(at(79.5)), "setelah jeda")
    for label in ("entry", "h1", "h4"):
        check_price(out, "wC2", "MintC", label, P(at(200)), "carry-forward <= 24 jam")
    check_price(out, "wC2", "MintC", "h24", None, "carry-forward > 24 jam = NULL (basi)")
    for label in ("entry", "h1", "h4", "h24"):
        check_price(out, "wC3", "MintC", label, None, "sangat basi = NULL")

    print("== MintD/E: 404 dan tanpa pool layak")
    check(run("MintD")[0] == "not_found", "MintD 404 -> not_found")
    check(run("MintE")[0] == "no_pool", "MintE (bukan base / quote asing) -> no_pool")
    check(out.execute("select count(*) from price_points where coin_mint in ('MintD','MintE')").fetchone()[0] == 0, "MintD/E tidak menulis harga")

    print("== MintF: 500, 429, 500, lalu sukses (retry)")
    th = s2.Throttle(0.01)
    st, _ = run("MintF", th)
    check(st == "ok" and th.rate_limited == 1, f"MintF pulih setelah error sementara (429 tercatat={th.rate_limited})")
    check_price(out, "wF1", "MintF", "h4", P(at(14.5)), "MintF")

    print("== MintH: waktu target di masa depan = NULL")
    run("MintH", now=at(30))
    check_price(out, "wH1", "MintH", "h4", P(at(14.5)), "MintH (masih masa lalu)")
    check_price(out, "wH1", "MintH", "h24", None, "MintH (34.5j > sekarang tiruan 30j)")

    print("== statistik per koin")
    row = out.execute("select status, n_wallets, entry_ok, h4_ok from coin_stats where coin_mint='MintA'").fetchone()
    check(row == ("ok", 3, 3, 3), f"coin_stats MintA {row}")

    print("== main(): resume 3 sesi == 1 sesi")
    for k in (1, 2, 3):
        add_pool(f"MintS{k}", f"PS{k}", 1e6, 0, candles(0, 100, 100))
    make_stage1(os.path.join(tmp, "s1_res.db"), [(f"wS{k}", f"MintS{k}", 10.5 + k) for k in (1, 2, 3)])
    args = ["--stage1-db", os.path.join(tmp, "s1_res.db"), "--interval", "0"]
    o_multi, o_single = os.path.join(tmp, "multi.db"), os.path.join(tmp, "single.db")
    quiet_main(args + ["--out", o_multi, "--max-coins", "1"])
    quiet_main(args + ["--out", o_multi, "--max-coins", "1"])
    code, _ = quiet_main(args + ["--out", o_multi])
    quiet_main(args + ["--out", o_single])
    check(code == 0 and dump_prices(o_multi) == dump_prices(o_single) and len(dump_prices(o_single)) == 12, "hasil 3 sesi identik dengan 1 sesi (12 baris)")

    print("== main(): berhenti setelah 8 koin gagal berturut-turut, lalu pulih")
    for k in range(1, 10):
        add_pool(f"MintErr{k}", f"PErr{k}", 1e6, 0, candles(0, 100, 100))
        FAIL[("pools", f"MintErr{k}")] = "always"
    make_stage1(os.path.join(tmp, "s1_err.db"), [(f"wErr{k}", f"MintErr{k}", 10.5) for k in range(1, 10)])
    o_err = os.path.join(tmp, "err.db")
    eargs = ["--stage1-db", os.path.join(tmp, "s1_err.db"), "--interval", "0", "--out", o_err]
    code, text = quiet_main(eargs)
    n_done = sqlite3.connect(o_err).execute("select count(*) from coin_stats").fetchone()[0]
    check(code == 2 and n_done == 0 and "BERHENTI" in text, f"exit 2, 0 koin dicatat selesai (exit={code}, selesai={n_done})")
    for k in range(1, 10):
        FAIL.pop(("pools", f"MintErr{k}"), None)
    code, _ = quiet_main(eargs)
    n_done = sqlite3.connect(o_err).execute("select count(*) from coin_stats where status='ok'").fetchone()[0]
    check(code == 0 and n_done == 9, f"setelah API pulih, run berikut menyelesaikan 9 koin (dapat {n_done})")

    print("== main(): tolak database peninggalan v1")
    v1 = os.path.join(tmp, "v1.db")
    db = sqlite3.connect(v1)
    db.executescript("create table coin_pools(coin_mint text primary key, pool_address text, status text);"
                     "create table price_points(wallet text, coin_mint text, horizon text, price_usd real, fetched_at text);")
    db.commit()
    db.close()
    code, text = quiet_main(["--stage1-db", s1p, "--out", v1, "--interval", "0"])
    tables = {r[0] for r in sqlite3.connect(v1).execute("select name from sqlite_master where type='table'")}
    check(code == 1 and "rm " in text and "coin_stats" not in tables, "v1 ditolak dengan petunjuk rm, file tidak diubah")

    print("== kompatibel dengan stage3")
    for k in range(1, 6):
        add_pool(f"MintQ{k}", f"PQ{k}", 1e6, 0, candles(0, 100, 100))
    q_rows = [("WQ", f"MintQ{k}", 10.5 + 3 * k) for k in range(1, 6)]
    make_stage1(os.path.join(tmp, "s1_q.db"), q_rows)
    o_q, o3 = os.path.join(tmp, "q2.db"), os.path.join(tmp, "q3.db")
    quiet_main(["--stage1-db", os.path.join(tmp, "s1_q.db"), "--interval", "0", "--out", o_q])
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "stage3_compute_scores.py"), "--stage1-db", os.path.join(tmp, "s1_q.db"),
                        "--stage2-db", o_q, "--out", o3], capture_output=True, text=True)
    expected = sum((P(at(h + 4)) - P(at(h))) / P(at(h)) * 100 for _, _, h in q_rows) / 5
    row = sqlite3.connect(o3).execute("select coins_scored, min_sample_met, hit_rate_h4, avg_return_h4 from wallet_scores where wallet='WQ'").fetchone() if r.returncode == 0 else None
    check(r.returncode == 0 and row is not None and row[0] == 5 and row[1] == 1 and row[2] == 0 and near(row[3], expected, 1e-6),
          f"stage3 membaca output stage2 (dapat {row}, avg harapan {expected:.6f}) {r.stderr[-200:]}")

    print()
    print("SEMUA TEST LOLOS" if not FAILS else f"{len(FAILS)} TEST GAGAL:\n  " + "\n  ".join(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
