-- Crypto-whales: skema inti
-- Prinsip desain (hasil audit SCIA sebelumnya):
--   1. Simpan angka ASLI atau NULL, jangan pernah nilai bawaan yang menyamar jadi data (kasus volume_sol=10.0 di SCIA)
--   2. Satu wallet + satu koin = satu baris kemunculan pertama, bukan dicatat ulang tiap scan
--   3. Skor dihitung dari hasil harga nyata, bukan dari skor buatan sistem sendiri (kasus AI Brain SCIA)
--   4. Field "is_bot_suspect" eksplisit, bukan cuma nebak dari volume

create table wallets (
  address             text primary key,
  first_seen_at       timestamptz not null,
  last_seen_at        timestamptz not null,
  coins_touched       integer not null default 0,      -- dihitung ulang, bukan dinaikkan manual per event
  is_bot_suspect       boolean not null default false,   -- true kalau pola: >N koin dengan sample kecil / muncul >N kali di 1 koin
  bot_suspect_reason  text,                              -- alasan tekstual, supaya bisa diaudit kenapa ditandai bot
  created_at          timestamptz not null default now()
);

create table coins (
  mint                text primary key,
  symbol              text,
  first_seen_at       timestamptz not null,
  is_dead             boolean not null default false,   -- true kalau harga sudah tidak bisa diambil (rug/delisted)
  created_at          timestamptz not null default now()
);

-- Satu baris = satu wallet pertama kali terlihat di satu koin.
-- BEDA dari SCIA: SCIA nulis baris baru tiap scan (10 detik), di sini di-upsert dan hanya first_seen yang dicatat.
create table appearances (
  id                  bigint generated always as identity primary key,
  wallet              text not null references wallets(address),
  coin_mint           text not null references coins(mint),
  first_seen_at       timestamptz not null,              -- waktu bot MELIHAT, bukan waktu transaksi on-chain
  tx_signature        text,                               -- diisi kalau tersedia, buat verifikasi waktu on-chain asli
  entry_rank          integer,                             -- urutan masuk di koin ini (1 = paling awal terlihat), dihitung belakangan
  native_sol_volume   numeric,                             -- NULL kalau tidak diketahui — JANGAN diisi nilai tebakan
  unique (wallet, coin_mint)
);

-- Snapshot harga koin pada beberapa titik waktu setelah kemunculan pertama.
-- Diisi oleh proses terpisah (cron), bukan saat appearance dicatat, karena harga +24h baru ada 24 jam kemudian.
create table price_snapshots (
  id                  bigint generated always as identity primary key,
  coin_mint           text not null references coins(mint),
  appearance_id       bigint not null references appearances(id),
  horizon             text not null check (horizon in ('h1','h4','h24')),
  price_usd           numeric,                             -- NULL kalau gagal diambil (koin mati, API error) — dicatat sebagai NULL, bukan 0
  fetched_at          timestamptz not null default now(),
  unique (appearance_id, horizon)
);

-- Skor per wallet, dihitung ULANG dari appearances + price_snapshots, bukan diakumulasi incremental.
-- Alasan: skor incremental gampang drift dan susah diaudit ulang; recompute penuh selalu bisa diverifikasi dari nol.
create table wallet_scores (
  wallet              text primary key references wallets(address),
  coins_scored        integer not null default 0,          -- berapa koin yang punya data harga cukup untuk dinilai
  hit_rate_h4         numeric,                              -- persentase koin yang naik > threshold pada +4h, NULL kalau coins_scored < min_sample
  avg_return_h4       numeric,                              -- rata-rata return %, bisa negatif
  min_sample_met      boolean not null default false,       -- eksplisit: skor ini layak dipercaya atau belum
  computed_at         timestamptz not null default now()
);

-- Sinyal konfluensi: N wallet berskor tinggi masuk koin yang sama dalam jendela waktu tertentu.
create table confluence_signals (
  id                  bigint generated always as identity primary key,
  coin_mint           text not null references coins(mint),
  wallet_count        integer not null,
  wallet_addresses    text[] not null,
  window_start        timestamptz not null,
  window_end          timestamptz not null,
  notified_at         timestamptz,                          -- NULL = belum dikirim ke Telegram
  created_at          timestamptz not null default now()
);

create index idx_appearances_coin on appearances(coin_mint, first_seen_at);
create index idx_appearances_wallet on appearances(wallet);
create index idx_price_snapshots_appearance on price_snapshots(appearance_id);
create index idx_confluence_unnotified on confluence_signals(coin_mint) where notified_at is null;
