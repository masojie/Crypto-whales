#!/usr/bin/env python3
"""
Crypto-whales — Helius On-Chain Extractor v3 (FINAL)
=====================================================
Ambil data on-chain langsung dari Helius RPC.

MODE:
  1. --token-mint <addr>      → lacak early buyers 1 token (✅ TERBUKTI WORKING)
  2. --token-list <file.txt>  → lacak banyak token dari file (1 mint per baris)
  3. --discover               → cari token baru (best-effort, experimental)

Output: SQLite DB identik dengan stage1 → bisa langsung di-feed ke stage2 + stage3.

Requirements: pip install requests (opsional, script pakai urllib bawaan)
"""
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "")   # dicek di main(), bukan saat import, supaya modul bisa dites
HELIUS    = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
SIG_PAGE = 1000        # tanda tangan per halaman getSignaturesForAddress
MAX_SIG_PAGES = 30     # batas 30.000 transaksi per token; lebih ramai dari itu = awal token tak terjangkau
PUMP_FUN  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOL_MINT  = "So11111111111111111111111111111111111111112"
TOKEN22   = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

class RpcError(Exception):
    """RPC gagal permanen. SENGAJA bukan hasil kosong: 'gagal' berbeda dari 'tidak ada data'."""


def rpc(method, params, quiet=True, attempts=4):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(HELIUS, data=body, headers={"Content-Type":"application/json"})
    for attempt in range(attempts):
        time.sleep(0.25 if attempt == 0 else min(2 ** attempt, 20))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                continue
            raise RpcError(f"HTTP {e.code} untuk {method} (cek HELIUS_API_KEY / kuota)")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if "error" in d:
            msg = str(d["error"].get("message", "?"))
            if d["error"].get("code") in (-32429, 429) or "rate" in msg.lower():
                continue
            raise RpcError(msg[:120])
        return d.get("result")
    raise RpcError(f"{method} gagal setelah {attempts} percobaan")

def get_sigs(addr, limit=100):
    r = rpc("getSignaturesForAddress", [addr, {"limit":limit}])
    return r if isinstance(r,list) else []

def get_all_sigs(addr):
    """Semua tanda tangan, dari yang TERBARU ke TERTUA. -> (daftar, lengkap).
    lengkap=False kalau MAX_SIG_PAGES tercapai sebelum awal token ketemu."""
    out, before = [], None
    for _ in range(MAX_SIG_PAGES):
        opts = {"limit": SIG_PAGE}
        if before:
            opts["before"] = before
        r = rpc("getSignaturesForAddress", [addr, opts])
        if not isinstance(r, list) or not r:
            return out, True
        out.extend(r)
        before = r[-1]["signature"]
        if len(r) < SIG_PAGE:
            return out, True
    return out, False

def get_tx(sig):
    r = rpc("getTransaction", [sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}], quiet=False)
    return r if isinstance(r,dict) and r else None

# ── Discovery ──────────────────────────────────
def discover_tokens(limit=20):
    """
    Cari token baru dengan 2 metode:
    A. Scan pump.fun signatures → cek token balance changes
    B. Scan Token-2022 program → cari initializeMint
    """
    tokens, seen = [], set()

    # Metode A: pump.fun
    sigs = get_sigs(PUMP_FUN, limit=limit*3)
    for info in sigs:
        if len(tokens) >= limit: break
        tx = get_tx(info["signature"])
        if not tx: continue
        new = _new_mints_from_balances(tx)
        for m in new:
            if m not in seen and m != SOL_MINT:
                seen.add(m)
                tokens.append({"mint":m,"created_at":_block_ts(info)})

    # Metode B: Token-2022
    if len(tokens) < limit:
        sigs2 = get_sigs(TOKEN22, limit=limit*3)
        for info in sigs2:
            if len(tokens) >= limit: break
            tx = get_tx(info["signature"])
            if not tx: continue
            m = _new_mint_from_tx(tx)
            if m and m not in seen and m != SOL_MINT:
                seen.add(m)
                tokens.append({"mint":m,"created_at":_block_ts(info)})

    return tokens

def _new_mints_from_balances(tx):
    meta = tx.get("meta",{})
    pre  = {b["mint"] for b in meta.get("preTokenBalances",[])}
    post = {b["mint"] for b in meta.get("postTokenBalances",[])}
    return list(post - pre)

def _new_mint_from_tx(tx):
    """Cari mint dari initializeMint (Token-2022 atau SPL Token)."""
    for src in [
        tx.get("transaction",{}).get("message",{}).get("instructions",[]),
        *[g.get("instructions",[]) for g in tx.get("meta",{}).get("innerInstructions",[])]
    ]:
        for ix in src:
            p = ix.get("parsed",{})
            if p.get("type") == "initializeMint":
                m = p.get("info",{}).get("mint","")
                if m: return m
    return None

def _block_ts(info):
    ts = info.get("blockTime",0)
    return datetime.fromtimestamp(ts,tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()

# ── Early Buyer Detection ──────────────────────
def find_buyers(mint, max_n=30):
    """Early buyer SUNGGUHAN: transaksi dibaca dari yang TERTUA. -> (buyers, lengkap).
    Kode lama membaca 200 transaksi TERBARU, jadi untuk token dengan lebih dari 30 pembeli
    hasilnya adalah pembeli TERAKHIR, bukan yang pertama."""
    sigs, complete = get_all_sigs(mint)
    buyers = {}
    for info in reversed(sigs):
        if len(buyers) >= max_n:
            break
        bt = info.get("blockTime")
        if not bt or info.get("err"):
            continue
        tx = get_tx(info["signature"])
        if not tx:
            continue
        for sw in _parse_buys(tx, mint):
            w = sw["buyer"]
            if w not in buyers:
                buyers[w] = {"wallet":w,"first_seen":datetime.fromtimestamp(bt,tz=timezone.utc).isoformat(),"first_seen_epoch":bt,"sol_volume":round(sw.get("sol_amount",0),4)}
    return sorted(buyers.values(), key=lambda x: x["first_seen_epoch"])[:max_n], complete

def _parse_buys(tx, target):
    swaps = []
    try:
        meta = tx.get("meta",{})
        keys = tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
        pre_map  = {(b.get("owner",""),b.get("mint","")): b.get("uiTokenAmount",{}).get("uiAmount",0) or 0 for b in meta.get("preTokenBalances",[])}
        post_map = {}
        for b in meta.get("postTokenBalances",[]):
            post_map[(b.get("owner",""),b.get("mint",""))] = b.get("uiTokenAmount",{}).get("uiAmount",0) or 0
        pre_sol  = meta.get("preBalances",[])
        post_sol = meta.get("postBalances",[])

        for (owner,mint), post_amt in post_map.items():
            if mint != target: continue
            pre_amt = pre_map.get((owner,mint),0)
            if post_amt <= pre_amt: continue
            sol = 0.0
            for i,k in enumerate(keys):
                k = k if isinstance(k,str) else k.get("pubkey","")
                if k == owner and i < len(pre_sol) and i < len(post_sol):
                    d = (pre_sol[i]-post_sol[i])/1e9
                    if d > 0: sol = round(d,4)
                    break
            swaps.append({"buyer":owner,"token_amount":round(post_amt-pre_amt,4),"sol_amount":sol})
    except (KeyError, TypeError, AttributeError, ValueError):
        pass
    return swaps

# ── Database ───────────────────────────────────
LO, HI = 3, 500

def init_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, coins_touched INTEGER NOT NULL, total_appearances INTEGER NOT NULL, is_bot_suspect INTEGER NOT NULL DEFAULT 0, bot_suspect_reason TEXT);
        CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY, symbol TEXT, first_seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS appearances(wallet TEXT NOT NULL, coin_mint TEXT NOT NULL, first_seen_at TEXT NOT NULL, native_sol_volume REAL, PRIMARY KEY(wallet, coin_mint));
    """)
    return db

def save(db, token, buyers):
    m,c = token["mint"], token["created_at"]
    db.execute("INSERT OR IGNORE INTO coins(mint,symbol,first_seen_at) VALUES(?,?,?)",(m,None,c))
    for b in buyers:
        w,fs,sv=b["wallet"],b["first_seen"],b.get("sol_volume",0)
        sv=sv if sv>0 else None
        ex=db.execute("SELECT first_seen_at FROM appearances WHERE wallet=? AND coin_mint=?",(w,m)).fetchone()
        if ex:
            if fs<ex[0]: db.execute("UPDATE appearances SET first_seen_at=?,native_sol_volume=? WHERE wallet=? AND coin_mint=?",(fs,sv,w,m))
        else: db.execute("INSERT INTO appearances(wallet,coin_mint,first_seen_at,native_sol_volume) VALUES(?,?,?,?)",(w,m,fs,sv))

def recompute_wallets(db):
    db.executescript("DELETE FROM wallets; INSERT INTO wallets(address,first_seen_at,last_seen_at,coins_touched,total_appearances) SELECT wallet,MIN(first_seen_at),MAX(first_seen_at),COUNT(DISTINCT coin_mint),COUNT(*) FROM appearances GROUP BY wallet;")
    # appearances sudah unik per (wallet, koin), jadi total_appearances == coins_touched dan aturan bot stage1
    # (<=3 koin tapi >=500 kemunculan) TIDAK PERNAH bisa terpenuhi di sini. Dihapus daripada pura-pura aktif.
    db.commit()

def summary(db, out_path):
    cc=db.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    wc=db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    ac=db.execute("SELECT COUNT(*) FROM appearances").fetchone()[0]
    bc=db.execute("SELECT COUNT(*) FROM wallets WHERE is_bot_suspect=1").fetchone()[0]
    print(f"\n{'='*50}\n📊 {cc} koin | {wc} wallet | {ac} appearances | 🤖{bc} bot suspect (deteksi bot TIDAK aktif di mode on-chain)\n💾 {out_path}")
    top=db.execute("SELECT address,coins_touched,total_appearances FROM wallets WHERE is_bot_suspect=0 ORDER BY coins_touched DESC LIMIT 5").fetchall()
    if top:
        print("\n🏆 Top wallets (non-bot):")
        for ad,co,ap in top: print(f"   {ad[:16]}... — {co} koin, {ap}x")

# ── Main ───────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Crypto-whales Helius Extractor")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--token-mint", help="Lacak 1 token")
    g.add_argument("--token-list", help="File .txt berisi daftar mint (1 per baris)")
    g.add_argument("--discover", action="store_true", help="Cari token baru (experimental)")
    p.add_argument("--max-tokens", type=int, default=5)
    p.add_argument("--max-buyers", type=int, default=30)
    p.add_argument("--allow-truncated", action="store_true", help="simpan juga token yang terlalu ramai (hasilnya BUKAN early buyer sungguhan)")
    p.add_argument("--out", default="stage1_onchain.db")
    a = p.parse_args()
    if not HELIUS_KEY:
        print("ERROR: Set HELIUS_API_KEY environment variable.", file=sys.stderr)
        return 1

    db = init_db(a.out)
    print(f"💾 Output: {a.out}\n")

    tokens_to_process = []

    if a.token_mint:
        tokens_to_process = [{"mint": a.token_mint, "created_at": datetime.now(timezone.utc).isoformat()}]

    elif a.token_list:
        with open(a.token_list) as f:
            for line in f:
                mint = line.strip()
                if mint and not mint.startswith("#"):
                    tokens_to_process.append({"mint": mint, "created_at": datetime.now(timezone.utc).isoformat()})
        print(f"📋 {len(tokens_to_process)} token dari {a.token_list}")

    elif a.discover:
        print("🔎 Mencari token baru...")
        try:
            tokens_to_process = discover_tokens(a.max_tokens)
        except RpcError as e:
            print(f"❌ Helius gagal: {e}")
            db.close()
            return 1
        if not tokens_to_process:
            print("❌ Token tidak ditemukan.\n💡 Tip: gunakan --token-mint <address> untuk mode single token yang sudah terbukti.")
            db.close()
            return 1
        print(f"✅ {len(tokens_to_process)} token ditemukan")

    failures = 0
    print(f"\n🔍 Melacak early buyers untuk {len(tokens_to_process)} token...\n")
    for i, token in enumerate(tokens_to_process):
        print(f"[{i+1}/{len(tokens_to_process)}] {token['mint'][:16]}...", end=" ", flush=True)
        try:
            buyers, complete = find_buyers(token["mint"], a.max_buyers)
        except RpcError as e:
            failures += 1
            print(f"→ GAGAL ({e}); token dilewati, tidak disimpan")
            if failures >= 3:
                print("BERHENTI: 3 token gagal berturut-turut. Cek HELIUS_API_KEY dan kuota Helius.", file=sys.stderr)
                break
            continue
        failures = 0
        if not complete and not a.allow_truncated:
            print(f"→ DILEWATI: token lebih ramai dari {MAX_SIG_PAGES * SIG_PAGE} transaksi, awalnya tak terjangkau (bukan early buyer). Pakai --allow-truncated kalau tetap mau.")
            continue
        print(f"→ {len(buyers)} early buyers" + ("" if complete else " (TERPOTONG, bukan early buyer sungguhan)"))
        if buyers:
            token["created_at"] = buyers[0]["first_seen"]
        save(db, token, buyers)
        if len(tokens_to_process) > 1:
            recompute_wallets(db)

    recompute_wallets(db)
    summary(db, a.out)
    db.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())