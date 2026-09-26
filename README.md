# Crypto-whales

Pelacak wallet early-buyer di Solana. Referensi arsitektur: [SCIA-SUPREME](https://github.com/masojie/SCIA-SUPREME),
tapi ditulis ulang dengan skema lebih ramping dan 3 bug yang ditemukan di SCIA sudah diperbaiki di desain:

1. **Volume asli, bukan nilai bawaan yang menyamar jadi data.** SCIA menulis `volume_sol = 10.0` untuk 99% baris
   sebagai tebakan, sehingga kolom itu nyaris tidak berarti. Di sini, kolom `native_sol_volume` bernilai `NULL`
   kalau tidak diketahui — tidak pernah diisi angka tebakan.
2. **Satu kemunculan per wallet per koin.** SCIA menulis baris baru tiap scan (~10 detik), membuat databasenya
   membengkak jadi 1.4 GB untuk 193 ribu wallet. Di sini, `appearances` di-upsert dengan `unique(wallet, coin_mint)`.
3. **Skor dari hasil harga nyata, bukan skor buatan sistem sendiri.** SCIA menghitung `action` dan `outcome` dari
   nilai yang sistemnya sendiri hasilkan (circular), sehingga "belajar" dari dirinya sendiri. Di sini, skor
   (`wallet_scores`) dihitung ulang dari `price_snapshots` yang diambil dari sumber harga eksternal.

## Status saat ini (jujur, per commit ini)

**Sudah ada dan sudah ditest:**
- `supabase/migrations/0001_init.sql` — skema database inti (5 tabel)
- `lib/scoring.ts` — fungsi murni penghitung skor early-buyer + deteksi wallet bot-suspect
- `lib/scoring.test.ts` — 9 kasus uji, termasuk kasus tepi (harga null, sample kurang, entry price 0, floating point)

**BELUM ada:**
- Tidak ada kode yang mengambil data on-chain (belum terhubung ke Helius atau sumber manapun)
- Tidak ada kode yang mengambil harga dari GeckoTerminal/DexScreener
- Tidak ada dashboard (halaman Next.js)
- Tidak ada koneksi ke Supabase yang sesungguhnya — migrasi belum pernah dijalankan ke instance manapun
- Tidak ada bot Telegram/notifikasi
- Tidak ada cron job
- Skema dan fungsi skor belum pernah diuji dengan data SCIA yang sesungguhnya (backup 1.4 GB), baru data tiruan.

Singkatnya: ini adalah fondasi (skema + logika skor yang teruji), bukan aplikasi yang bisa dipakai.
Jangan anggap ini "siap pakai" — langkah berikutnya adalah menyambungkan fondasi ini ke data nyata.

## Menjalankan test

```bash
npm install
npx tsx lib/scoring.test.ts
```

## Parameter yang bisa disesuaikan (lib/scoring.ts)

- `MIN_SAMPLE = 5` — wallet perlu minimal 5 koin dengan data harga lengkap sebelum skornya ditampilkan
- `HIT_THRESHOLD_PCT = 20` — kenaikan harga dianggap "hit" kalau >= 20% pada +4 jam
- Deteksi bot-suspect: wallet dengan <=3 koin unik TAPI >=500 kemunculan ditandai sebagai kemungkinan bot,
  bukan early-buyer manusiawi. Ini heuristik dari pola yang ditemukan saat audit database SCIA (wallet yang
  muncul 13.000+ kali di 1 koin), bukan bukti pasti.
