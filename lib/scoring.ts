// Fungsi skor early-buyer.
// Sengaja ditulis sebagai fungsi murni (tidak menyentuh database langsung),
// supaya bisa ditest dengan data tiruan sebelum dipakai di produksi.

export type Appearance = {
  wallet: string;
  coinMint: string;
  entryRank: number | null; // 1 = paling awal terlihat di koin itu
};

export type PriceSnapshot = {
  coinMint: string;
  appearanceWalletMint: string; // key gabungan wallet:coinMint, biar gampang di-lookup
  horizon: 'h1' | 'h4' | 'h24';
  priceUsd: number | null; // null = gagal diambil / koin mati
};

export type EntryPrice = {
  coinMint: string;
  wallet: string;
  priceAtEntryUsd: number | null;
};

export type WalletScoreResult = {
  wallet: string;
  coinsScored: number;
  hitRateH4: number | null;
  avgReturnH4: number | null;
  minSampleMet: boolean;
};

const MIN_SAMPLE = 5; // wallet harus punya minimal 5 koin dengan data harga lengkap sebelum dipercaya
const HIT_THRESHOLD_PCT = 20; // dianggap "hit" kalau harga naik >= 20% pada +4 jam
const FLOAT_EPSILON = 1e-9; // toleransi floating point — return persis di ambang batas harus konsisten dihitung hit,
                             // bukan tergantung arah pembulatan (lihat test kasus 8: 1.0 -> 1.2 bisa jadi 19.999999999999996)

/**
 * Menghitung return % satu wallet di satu koin pada horizon h4.
 * Mengembalikan null (bukan 0) kalau data harga tidak lengkap — 0 dan "tidak ada data" harus dibedakan.
 */
function computeReturnPct(entryPrice: number | null, priceH4: number | null): number | null {
  if (entryPrice === null || priceH4 === null) return null;
  if (entryPrice <= 0) return null; // hindari pembagian oleh nol/negatif
  return ((priceH4 - entryPrice) / entryPrice) * 100;
}

/**
 * Fungsi inti: hitung skor semua wallet dari daftar entry+harga.
 * Ini FUNGSI MURNI — tidak query Supabase, tidak fetch API. Dipanggil oleh job terpisah
 * yang menyiapkan `entries` dan `snapshots` dari database, lalu menyimpan hasilnya balik.
 */
export function computeWalletScores(
  entries: EntryPrice[],
  snapshotsH4: Map<string, number | null> // key: `${wallet}:${coinMint}` -> priceUsd h4
): WalletScoreResult[] {
  const byWallet = new Map<string, number[]>(); // wallet -> daftar return% yang valid (bukan null)
  const coinsSeenByWallet = new Map<string, number>(); // wallet -> jumlah koin yang PUNYA data harga (valid atau tidak hit)

  for (const entry of entries) {
    const key = `${entry.wallet}:${entry.coinMint}`;
    const priceH4 = snapshotsH4.get(key) ?? null;
    const ret = computeReturnPct(entry.priceAtEntryUsd, priceH4);

    if (ret === null) continue; // data tidak lengkap, koin ini tidak dihitung sama sekali untuk wallet ini

    coinsSeenByWallet.set(entry.wallet, (coinsSeenByWallet.get(entry.wallet) ?? 0) + 1);
    const list = byWallet.get(entry.wallet) ?? [];
    list.push(ret);
    byWallet.set(entry.wallet, list);
  }

  const results: WalletScoreResult[] = [];
  for (const [wallet, returns] of byWallet.entries()) {
    const coinsScored = coinsSeenByWallet.get(wallet) ?? 0;
    const minSampleMet = coinsScored >= MIN_SAMPLE;

    const hits = returns.filter((r) => r >= HIT_THRESHOLD_PCT - FLOAT_EPSILON).length;
    const hitRateH4 = minSampleMet ? (hits / returns.length) * 100 : null;
    const avgReturnH4 = minSampleMet
      ? returns.reduce((a, b) => a + b, 0) / returns.length
      : null;

    results.push({
      wallet,
      coinsScored,
      hitRateH4,
      avgReturnH4,
      minSampleMet,
    });
  }

  return results;
}

/**
 * Deteksi wallet yang polanya mirip bot, bukan early buyer manusiawi.
 * Kriteria (hasil temuan audit SCIA): wallet muncul di sangat sedikit koin (<=2)
 * TAPI jumlah kemunculannya di koin itu sangat tinggi — pola sniper/MEV bot, bukan trader.
 * Ini heuristik, bukan bukti pasti — makanya field-nya "is_bot_suspect", bukan "is_bot".
 */
export function detectBotSuspect(
  coinsTouched: number,
  totalAppearances: number
): { suspect: boolean; reason: string | null } {
  const LOW_COIN_COUNT = 3;
  const HIGH_APPEARANCE_COUNT = 500;

  if (coinsTouched <= LOW_COIN_COUNT && totalAppearances >= HIGH_APPEARANCE_COUNT) {
    return {
      suspect: true,
      reason: `Muncul ${totalAppearances}x tapi hanya di ${coinsTouched} koin — pola sniper/bot, bukan early buyer`,
    };
  }
  return { suspect: false, reason: null };
}
