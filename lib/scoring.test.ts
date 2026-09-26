import { computeWalletScores, detectBotSuspect, EntryPrice } from './scoring';

let failed = 0;
function assertEqual(actual: unknown, expected: unknown, label: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    console.error(`GAGAL  ${label}\n  dapat:    ${a}\n  harapan:  ${e}`);
    failed++;
  } else {
    console.log(`lolos  ${label}`);
  }
}

// Kasus 1: wallet dengan 5 koin, semua data lengkap, 3 dari 5 naik >=20% -> harusnya minSampleMet true
{
  const entries: EntryPrice[] = [
    { wallet: 'W1', coinMint: 'C1', priceAtEntryUsd: 1.0 },
    { wallet: 'W1', coinMint: 'C2', priceAtEntryUsd: 1.0 },
    { wallet: 'W1', coinMint: 'C3', priceAtEntryUsd: 1.0 },
    { wallet: 'W1', coinMint: 'C4', priceAtEntryUsd: 1.0 },
    { wallet: 'W1', coinMint: 'C5', priceAtEntryUsd: 1.0 },
  ];
  const snapshots = new Map([
    ['W1:C1', 1.5],  // +50% -> hit
    ['W1:C2', 1.25], // +25% -> hit
    ['W1:C3', 1.21], // +21% -> hit
    ['W1:C4', 1.10], // +10% -> bukan hit
    ['W1:C5', 0.9],  // -10% -> bukan hit
  ]);
  const result = computeWalletScores(entries, snapshots);
  assertEqual(result.length, 1, 'Kasus 1: jumlah wallet ternilai');
  assertEqual(result[0].minSampleMet, true, 'Kasus 1: sample terpenuhi (5 koin)');
  assertEqual(result[0].coinsScored, 5, 'Kasus 1: coinsScored = 5');
  assertEqual(result[0].hitRateH4, 60, 'Kasus 1: hit rate 3/5 = 60%');
}

// Kasus 2: wallet dengan cuma 3 koin data lengkap -> minSampleMet HARUS false, hitRate HARUS null
// Ini kasus paling penting: skor tidak boleh ditampilkan seolah valid kalau sample kurang.
{
  const entries: EntryPrice[] = [
    { wallet: 'W2', coinMint: 'C1', priceAtEntryUsd: 1.0 },
    { wallet: 'W2', coinMint: 'C2', priceAtEntryUsd: 1.0 },
    { wallet: 'W2', coinMint: 'C3', priceAtEntryUsd: 1.0 },
  ];
  const snapshots = new Map([
    ['W2:C1', 5.0], // +400%, kelihatan bagus banget
    ['W2:C2', 5.0],
    ['W2:C3', 5.0],
  ]);
  const result = computeWalletScores(entries, snapshots);
  assertEqual(result[0].minSampleMet, false, 'Kasus 2: sample TIDAK terpenuhi (cuma 3 koin)');
  assertEqual(result[0].hitRateH4, null, 'Kasus 2: hitRate null walau return kelihatan bagus');
  assertEqual(result[0].avgReturnH4, null, 'Kasus 2: avgReturn null walau return kelihatan bagus');
}

// Kasus 3: harga h4 null (data gagal diambil) -> koin ini TIDAK dihitung sama sekali, bukan dianggap 0%
{
  const entries: EntryPrice[] = [
    { wallet: 'W3', coinMint: 'C1', priceAtEntryUsd: 1.0 },
    { wallet: 'W3', coinMint: 'C2', priceAtEntryUsd: 1.0 }, // ini akan null di snapshot
  ];
  const snapshots = new Map([
    ['W3:C1', 1.3],
    // W3:C2 sengaja tidak ada di map (simulasi gagal fetch / koin mati)
  ]);
  const result = computeWalletScores(entries, snapshots);
  assertEqual(result[0].coinsScored, 1, 'Kasus 3: koin dengan harga null TIDAK dihitung (coinsScored=1, bukan 2)');
}

// Kasus 4: entry price 0 atau negatif -> harus diabaikan, bukan crash / infinity
{
  const entries: EntryPrice[] = [
    { wallet: 'W4', coinMint: 'C1', priceAtEntryUsd: 0 },
    { wallet: 'W4', coinMint: 'C2', priceAtEntryUsd: -1 },
  ];
  const snapshots = new Map([
    ['W4:C1', 1.0],
    ['W4:C2', 1.0],
  ]);
  const result = computeWalletScores(entries, snapshots);
  assertEqual(result.length, 0, 'Kasus 4: entry price 0/negatif -> wallet tidak muncul di hasil sama sekali');
}

// Kasus 5: deteksi bot -- muncul 1000x tapi cuma di 2 koin
{
  const r = detectBotSuspect(2, 1000);
  assertEqual(r.suspect, true, 'Kasus 5: wallet 1000x di 2 koin terdeteksi bot suspect');
}

// Kasus 6: wallet normal -- muncul 50x di 30 koin (early buyer aktif tapi wajar) -> BUKAN bot
{
  const r = detectBotSuspect(30, 50);
  assertEqual(r.suspect, false, 'Kasus 6: wallet aktif wajar (30 koin/50 muncul) TIDAK ditandai bot');
}

// Kasus 7: daftar entries kosong -> tidak boleh crash, harus balikin array kosong
{
  const result = computeWalletScores([], new Map());
  assertEqual(result, [], 'Kasus 7: entries kosong -> hasil array kosong, tidak crash');
}

// Kasus 8: wallet dengan return persis di ambang batas (20%) -> harus dihitung sebagai hit (>=), bukan meleset karena floating point
{
  const entries: EntryPrice[] = [
    { wallet: 'W8', coinMint: 'C1', priceAtEntryUsd: 1.0 },
    { wallet: 'W8', coinMint: 'C2', priceAtEntryUsd: 1.0 },
    { wallet: 'W8', coinMint: 'C3', priceAtEntryUsd: 1.0 },
    { wallet: 'W8', coinMint: 'C4', priceAtEntryUsd: 1.0 },
    { wallet: 'W8', coinMint: 'C5', priceAtEntryUsd: 1.0 },
  ];
  const snapshots = new Map([
    ['W8:C1', 1.2], // persis +20%
    ['W8:C2', 1.0],
    ['W8:C3', 1.0],
    ['W8:C4', 1.0],
    ['W8:C5', 1.0],
  ]);
  const result = computeWalletScores(entries, snapshots);
  assertEqual(result[0].hitRateH4, 20, 'Kasus 8: return persis +20% dihitung sebagai hit (ambang inklusif)');
}

console.log(`\n${failed === 0 ? 'SEMUA TEST LOLOS' : `${failed} TEST GAGAL`}`);
process.exit(failed === 0 ? 0 : 1);
