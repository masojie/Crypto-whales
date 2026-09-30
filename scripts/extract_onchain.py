#!/usr/bin/env python3
"""
Crypto-whales — Helius On-Chain Extractor v4
============================================
Ambil early buyer langsung dari Helius. Keluaran: SQLite dengan skema SAMA seperti
stage1 (wallets, coins, appearances), jadi bisa langsung masuk stage2 + stage3.

MODE:
  1. --token-mint <addr>      -> lacak early buyer 1 token
  2. --token-list <file.txt>  -> lacak banyak token (1 mint per baris)
  3. --discover               -> cari token baru (best-effort, eksperimental)

Perubahan dari v3:
  - Transaksi dibaca dari yang PALING AWAL (getTransactionsForAddress, sortOrder=asc).
    v3 memakai getSignaturesForAddress yang mengembalikan transaksi TERBARU dulu,
    jadi untuk token berumur jam-an hasilnya pembeli terkini, bukan early buyer.
  - Parse via Enhanced Transactions API (100 tx per panggilan), bukan 1 getTransaction
    per signature. Jauh lebih hemat kredit.
  - total_appearances kini = jumlah transaksi beli. Sebelumnya sama dengan coins_touched,
    sehingga deteksi bot (coins<=3 dan muncul>=500) tidak pernah bisa aktif.
  - coins.first_seen_at = waktu on-chain transaksi pertama token, bukan waktu skrip jalan.
  - Cek HELIUS_API_KEY dipindah ke main(), jadi modul bisa di-import/di-test.
  - Error parsing tidak lagi ditelan diam-diam.

Kunci API dibaca dari env HELIUS_API_KEY, tidak pernah ditulis ke file.
Hanya butuh Python standar (urllib), cocok untuk Termux.
Catatan: User-Agent khusus wajib; UA bawaan urllib ditolak (HTTP 403) oleh api.helius.xyz.
"""
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
LO, HI = 3, 500  # ambang bot-suspect, sama dengan stage1

_calls = {"rpc": 0, "parse": 0}


def helius_key():
    k = os.environ.get("HELIUS_API_KEY", "")
    if not k:
        raise SystemExit("ERROR: Set HELIUS_API_KEY environment variable.")
    return k


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _post(url, payload, retries=4):
    body = json.dumps(payload).encode()
    for i in range(retries):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "crypto-whales/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (i + 1))
                continue
            print(f"  HTTP {e.code} dari {url.split('?')[0]}: {e.read()[:100]!r}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  jaringan: {type(e).__name__}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    return None


def rpc(method, params, quiet=True):
    _calls["rpc"] += 1
    d = _post(f"https://mainnet.helius-rpc.com/?api-key={helius_key()}",
              {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not d:
        return {}
    if "error" in d:
        if not quiet:
            print(f"  RPC: {str(d['error'].get('message', '?'))[:80]}", file=sys.stderr)
        return {}
    return d.get("result", {})


def get_sigs(addr, limit=100):
    r = rpc("getSignaturesForAddress", [addr, {"limit": limit}])
    return r if isinstance(r, list) else []


def get_tx(sig):
    r = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], quiet=False)
    return r if isinstance(r, dict) and r else None


# -- Discovery (tidak diubah dari v3, eksperimental) ------------------------
def discover_tokens(limit=20):
    tokens, seen = [], set()
    for prog in (PUMP_FUN, TOKEN22):
        if len(tokens) >= limit:
            break
        for info in get_sigs(prog, limit=limit * 3):
            if len(tokens) >= limit:
                break
            tx = get_tx(info["signature"])
            if not tx:
                continue
            mints = _new_mints_from_balances(tx) if prog == PUMP_FUN else [m for m in [_new_mint_from_tx(tx)] if m]
            for m in mints:
                if m not in seen and m != SOL_MINT:
                    seen.add(m)
                    tokens.append({"mint": m, "created_at": _block_ts(info)})
    return tokens


def _new_mints_from_balances(tx):
    meta = tx.get("meta", {})
    pre = {b["mint"] for b in meta.get("preTokenBalances", [])}
    post = {b["mint"] for b in meta.get("postTokenBalances", [])}
    return list(post - pre)


def _new_mint_from_tx(tx):
    for src in [tx.get("transaction", {}).get("message", {}).get("instructions", []),
                *[g.get("instructions", []) for g in tx.get("meta", {}).get("innerInstructions", [])]]:
        for ix in src:
            p = ix.get("parsed", {})
            if p.get("type") == "initializeMint":
                m = p.get("info", {}).get("mint", "")
                if m:
                    return m
    return None


def _block_ts(info):
    ts = info.get("blockTime", 0)
    return iso(ts) if ts else iso(int(time.time()))


# -- Early buyer: baca dari yang PALING AWAL --------------------------------
def earliest_pages(mint, page_size, max_pages):
    """Yield halaman list signature (sukses saja), urut dari transaksi tertua."""
    token = None
    for _ in range(max_pages):
        opts = {"transactionDetails": "signatures", "sortOrder": "asc", "limit": page_size}
        if token:
            opts["paginationToken"] = token
        res = rpc("getTransactionsForAddress", [mint, opts], quiet=False)
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            return
        yield [x for x in data if not x.get("err")]
        token = res.get("paginationToken")
        if not token:
            return


def parse_txs(sigs):
    """Enhanced Transactions API: 100 signature per panggilan."""
    out = []
    for i in range(0, len(sigs), 100):
        _calls["parse"] += 1
        d = _post(f"https://api.helius.xyz/v0/transactions?api-key={helius_key()}",
                  {"transactions": sigs[i:i + 100]})
        if not isinstance(d, list):
            print("  parse gagal untuk 1 batch (dilewati)", file=sys.stderr)
            continue
        out.extend(d)
    out.sort(key=lambda t: t.get("timestamp", 0))
    return out


def _parse_buys(tx, mint):
    """Pembeli = fee payer yang MENERIMA token koin. Volume SOL hanya diisi bila
    Helius memberi swap.nativeInput; selain itu None (tidak ditebak)."""
    payer, ts = tx.get("feePayer"), tx.get("timestamp")
    if not payer or not ts:
        return []
    got = sum(float(t.get("tokenAmount") or 0) for t in tx.get("tokenTransfers", [])
              if t.get("mint") == mint and t.get("toUserAccount") == payer)
    if got <= 0:
        return []
    vol = None
    ni = ((tx.get("events") or {}).get("swap") or {}).get("nativeInput")
    if ni and ni.get("account") == payer and ni.get("amount"):
        vol = round(int(ni["amount"]) / 1e9, 4)
    return [{"buyer": payer, "ts": ts, "sol_volume": vol}]


def find_buyers(mint, max_n=30, page_size=300, max_pages=3):
    """Return (buyers_terurut_awal, jumlah_beli_per_wallet, ts_transaksi_pertama)."""
    buyers, counts, first_ts = {}, {}, None
    for page in earliest_pages(mint, page_size, max_pages):
        if first_ts is None and page:
            first_ts = page[0].get("blockTime")
        for tx in parse_txs([x["signature"] for x in page]):
            for b in _parse_buys(tx, mint):
                w = b["buyer"]
                counts[w] = counts.get(w, 0) + 1
                if w not in buyers:
                    buyers[w] = {"wallet": w, "first_seen": iso(b["ts"]), "first_seen_epoch": b["ts"],
                                 "sol_volume": b["sol_volume"]}
        if len(buyers) >= max_n:
            break
    early = sorted(buyers.values(), key=lambda x: x["first_seen_epoch"])[:max_n]
    return early, counts, first_ts


# -- Database ---------------------------------------------------------------
def init_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, coins_touched INTEGER NOT NULL, total_appearances INTEGER NOT NULL, is_bot_suspect INTEGER NOT NULL DEFAULT 0, bot_suspect_reason TEXT);
        CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY, symbol TEXT, first_seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS appearances(wallet TEXT NOT NULL, coin_mint TEXT NOT NULL, first_seen_at TEXT NOT NULL, native_sol_volume REAL, PRIMARY KEY(wallet, coin_mint));
        CREATE TABLE IF NOT EXISTS appearance_counts(wallet TEXT NOT NULL, coin_mint TEXT NOT NULL, n_buys INTEGER NOT NULL, PRIMARY KEY(wallet, coin_mint));
    """)
    return db


def save(db, token, buyers, counts):
    m = token["mint"]
    db.execute("INSERT OR IGNORE INTO coins(mint,symbol,first_seen_at) VALUES(?,?,?)", (m, None, token["created_at"]))
    for b in buyers:
        w, fs, sv = b["wallet"], b["first_seen"], b.get("sol_volume")
        ex = db.execute("SELECT first_seen_at FROM appearances WHERE wallet=? AND coin_mint=?", (w, m)).fetchone()
        if ex:
            if fs < ex[0]:
                db.execute("UPDATE appearances SET first_seen_at=?,native_sol_volume=? WHERE wallet=? AND coin_mint=?", (fs, sv, w, m))
        else:
            db.execute("INSERT INTO appearances(wallet,coin_mint,first_seen_at,native_sol_volume) VALUES(?,?,?,?)", (w, m, fs, sv))
        db.execute("INSERT OR REPLACE INTO appearance_counts(wallet,coin_mint,n_buys) VALUES(?,?,?)", (w, m, counts.get(w, 1)))
    db.commit()


def recompute_wallets(db):
    db.executescript("""
        DELETE FROM wallets;
        INSERT INTO wallets(address,first_seen_at,last_seen_at,coins_touched,total_appearances)
        SELECT a.wallet, MIN(a.first_seen_at), MAX(a.first_seen_at), COUNT(DISTINCT a.coin_mint),
               COALESCE((SELECT SUM(c.n_buys) FROM appearance_counts c WHERE c.wallet=a.wallet), COUNT(*))
        FROM appearances a GROUP BY a.wallet;
    """)
    for a, c, t in db.execute("SELECT address,coins_touched,total_appearances FROM wallets").fetchall():
        if c <= LO and t >= HI:
            db.execute("UPDATE wallets SET is_bot_suspect=1,bot_suspect_reason=? WHERE address=?",
                       (f"Muncul {t}x di {c} koin — bot/sniper", a))
    db.commit()


def summary(db, out_path):
    cc = db.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    wc = db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    ac = db.execute("SELECT COUNT(*) FROM appearances").fetchone()[0]
    bc = db.execute("SELECT COUNT(*) FROM wallets WHERE is_bot_suspect=1").fetchone()[0]
    print(f"\n{'=' * 50}\n📊 {cc} koin | {wc} wallet | {ac} appearances | 🤖{bc} bot suspect")
    print(f"📡 Panggilan Helius: {_calls['rpc']} RPC + {_calls['parse']} parse\n💾 {out_path}")
    top = db.execute("SELECT address,coins_touched,total_appearances FROM wallets WHERE is_bot_suspect=0 ORDER BY coins_touched DESC, total_appearances DESC LIMIT 5").fetchall()
    if top and top[0][1] > 1:
        print("\n🏆 Top wallets (non-bot):")
        for ad, co, ap in top:
            print(f"   {ad[:16]}... — {co} koin, {ap}x beli")


# -- Main -------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Crypto-whales Helius Extractor v4")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--token-mint", help="Lacak 1 token")
    g.add_argument("--token-list", help="File .txt berisi daftar mint (1 per baris)")
    g.add_argument("--discover", action="store_true", help="Cari token baru (experimental)")
    p.add_argument("--max-tokens", type=int, default=5)
    p.add_argument("--max-buyers", type=int, default=30)
    p.add_argument("--scan-sigs", type=int, default=300, help="transaksi tertua per halaman (maks 1000)")
    p.add_argument("--max-pages", type=int, default=3, help="maks halaman per token")
    p.add_argument("--out", default="stage1_onchain.db")
    a = p.parse_args()
    helius_key()  # gagal cepat kalau key kosong

    db = init_db(a.out)
    print(f"💾 Output: {a.out}\n")
    tokens = []
    if a.token_mint:
        tokens = [{"mint": a.token_mint, "created_at": None}]
    elif a.token_list:
        with open(a.token_list) as f:
            tokens = [{"mint": ln.strip(), "created_at": None} for ln in f if ln.strip() and not ln.startswith("#")]
        print(f"📋 {len(tokens)} token dari {a.token_list}")
    else:
        print("🔎 Mencari token baru...")
        tokens = discover_tokens(a.max_tokens)
        if not tokens:
            print("❌ Token tidak ditemukan.\n💡 Gunakan --token-mint <address>.")
            db.close()
            return 1
        print(f"✅ {len(tokens)} token ditemukan")

    print(f"\n🔍 Melacak early buyers untuk {len(tokens)} token...\n")
    for i, token in enumerate(tokens):
        print(f"[{i + 1}/{len(tokens)}] {token['mint'][:16]}...", end=" ", flush=True)
        buyers, counts, first_ts = find_buyers(token["mint"], a.max_buyers, min(a.scan_sigs, 1000), a.max_pages)
        if first_ts:
            token["created_at"] = iso(first_ts)
        elif buyers:
            token["created_at"] = buyers[0]["first_seen"]
        if not buyers:
            print("→ 0 buyers (dilewati)")
            continue
        print(f"→ {len(buyers)} buyers")
        save(db, token, buyers, counts)
    recompute_wallets(db)
    summary(db, a.out)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
