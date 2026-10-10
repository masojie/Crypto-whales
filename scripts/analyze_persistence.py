#!/usr/bin/env python3
"""
Uji persistensi (out-of-sample): apakah wallet dengan hit rate tinggi di paruh AWAL waktu tetap
tinggi di paruh AKHIR? Hit = naik >= 20% dalam 4 jam setelah wallet terlihat masuk.
Read-only pada stage1 + stage2. Pakai: python3 scripts/analyze_persistence.py --stage1-db s1.db --stage2-db s2.db
Peringatan: wallet yang masuk koin yang sama pada waktu berdekatan punya hasil yang berkorelasi, jadi
ukuran sampel efektif jauh lebih kecil dari jumlah pasangan. Angka z di bawah terlalu optimis.
"""
import argparse
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stage2_fetch_prices import parse_epoch

HIT, EPS = 20.0, 1e-9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1-db", required=True)
    ap.add_argument("--stage2-db", required=True)
    ap.add_argument("--min-train", type=int, default=3)
    ap.add_argument("--top-frac", type=float, default=0.2)
    a = ap.parse_args()
    s1 = sqlite3.connect(f"file:{a.stage1_db}?mode=ro", uri=True)
    s2 = sqlite3.connect(f"file:{a.stage2_db}?mode=ro", uri=True)
    price = {(w, m, h): p for w, m, h, p in s2.execute("select wallet, coin_mint, horizon, price_usd from price_points where horizon in ('entry','h4')")}
    pairs = []
    for w, m, t in s1.execute("select wallet, coin_mint, first_seen_at from appearances"):
        e, f, ts = price.get((w, m, "entry")), price.get((w, m, "h4")), parse_epoch(t)
        if e is None or f is None or e <= 0 or ts is None:
            continue
        pairs.append((ts, w, (f - e) / e * 100 >= HIT - EPS))
    if len(pairs) < 100:
        print(f"Terlalu sedikit pasangan berharga ({len(pairs)}). Jalankan stage2 untuk lebih banyak koin.")
        return 1
    pairs.sort()
    split = pairs[len(pairs) // 2][0]
    train = [p for p in pairs if p[0] < split]
    test = [p for p in pairs if p[0] >= split]
    base = sum(h for _, _, h in test) / len(test)
    tr, te = {}, {}
    for _, w, h in train:
        n, k = tr.get(w, (0, 0)); tr[w] = (n + 1, k + h)
    for _, w, h in test:
        n, k = te.get(w, (0, 0)); te[w] = (n + 1, k + h)
    cand = sorted((w for w, (n, _) in tr.items() if n >= a.min_train), key=lambda w: (-tr[w][1] / tr[w][0], -tr[w][0]))
    print(f"Pasangan berharga: {len(pairs)} (train {len(train)}, test {len(test)}). Hit base rate test: {100 * base:.1f}%")
    print(f"Wallet dengan >= {a.min_train} pasangan di train: {len(cand)}")
    if len(cand) < 10:
        print("Terlalu sedikit wallet kandidat untuk uji ini.")
        return 1
    k = max(1, int(len(cand) * a.top_frac))
    for name, group in (("TOP", cand[:k]), ("BOTTOM", cand[-k:])):
        n = sum(te[w][0] for w in group if w in te)
        hits = sum(te[w][1] for w in group if w in te)
        used = sum(1 for w in group if w in te)
        if n == 0:
            print(f"{name} {k} wallet train: tidak ada yang muncul di test"); continue
        rate = hits / n
        z = (rate - base) / math.sqrt(base * (1 - base) / n) if 0 < base < 1 else float("nan")
        tr_rate = sum(tr[w][1] for w in group) / sum(tr[w][0] for w in group)
        print(f"{name} {k} wallet (hit train {100 * tr_rate:.1f}%): {used} muncul di test, {n} pasangan, hit test {100 * rate:.1f}% vs base {100 * base:.1f}% (z={z:+.1f}, terlalu optimis karena korelasi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
