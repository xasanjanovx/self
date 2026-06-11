-- Migration 001: move profiles/settings out of the goals.title JSON hack
-- into dedicated tables. Safe to run multiple times (idempotent).
--
-- STATUS: prepared migration. The bot still reads/writes the legacy
-- goals.title storage today. Adopt in a coordinated step:
--   1. Run this file in Supabase -> SQL Editor (creates tables + backfills data).
--   2. Tell the developer; the db layer is then switched to these tables
--      and verified against your real data (legacy stays as a safe fallback).
-- Running this file alone is harmless: it only creates tables and copies data,
-- it does not change current bot behaviour.
--
-- NOTE: these tables are created WITHOUT a name prefix to match the existing
-- schema.sql. If you run the bot with DB_TABLE_PREFIX set, add the same prefix
-- to the table names here.

create table if not exists nutrition_profiles (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  mode text,
  title text,
  daily_calories integer not null default 0,
  protein numeric,
  fat numeric,
  carbs numeric,
  weight numeric,
  height numeric,
  age integer,
  bmi numeric,
  tdee integer,
  updated_at timestamptz not null default now()
);

create table if not exists finance_settings (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  card_base numeric not null default 0,
  cash_base numeric not null default 0,
  lent_base numeric not null default 0,
  debt_base numeric not null default 0,
  monthly_credit_payment numeric not null default 0,
  updated_at timestamptz not null default now()
);

create table if not exists report_preferences (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  enabled boolean not null default true,
  frequency text not null default 'weekly',
  last_sent_key text,
  updated_at timestamptz not null default now()
);

-- ---- Backfill existing data from goals.title JSON ----

-- Nutrition profiles (prefix "NUTRI_V1:")
insert into nutrition_profiles (telegram_id, mode, title, daily_calories, protein, fat, carbs, weight, height, age, bmi, tdee)
select
  g.telegram_id,
  (j ->> 'mode'),
  (j ->> 'title'),
  coalesce((j ->> 'daily_calories')::numeric, g.target_value, 0)::int,
  (j ->> 'protein')::numeric,
  (j ->> 'fat')::numeric,
  (j ->> 'carbs')::numeric,
  (j ->> 'weight')::numeric,
  (j ->> 'height')::numeric,
  (j ->> 'age')::int,
  (j ->> 'bmi')::numeric,
  (j ->> 'tdee')::int
from goals g
cross join lateral (
  select (substring(g.title from char_length('NUTRI_V1:') + 1))::jsonb as j
) parsed
where g.title like 'NUTRI_V1:%'
on conflict (telegram_id) do nothing;

-- Finance settings (prefix "FINANCE_PREF_V1:")
insert into finance_settings (telegram_id, card_base, cash_base, lent_base, debt_base, monthly_credit_payment)
select
  g.telegram_id,
  coalesce((j ->> 'card_base')::numeric, 0),
  coalesce((j ->> 'cash_base')::numeric, 0),
  coalesce((j ->> 'lent_base')::numeric, 0),
  coalesce((j ->> 'debt_base')::numeric, 0),
  coalesce((j ->> 'monthly_credit_payment')::numeric, 0)
from goals g
cross join lateral (
  select (substring(g.title from char_length('FINANCE_PREF_V1:') + 1))::jsonb as j
) parsed
where g.title like 'FINANCE_PREF_V1:%'
on conflict (telegram_id) do nothing;

-- Report preferences (prefix "REPORT_PREF_V1:")
insert into report_preferences (telegram_id, enabled, frequency, last_sent_key)
select
  g.telegram_id,
  coalesce((j ->> 'enabled')::boolean, true),
  coalesce((j ->> 'frequency'), 'weekly'),
  (j ->> 'last_sent_key')
from goals g
cross join lateral (
  select (substring(g.title from char_length('REPORT_PREF_V1:') + 1))::jsonb as j
) parsed
where g.title like 'REPORT_PREF_V1:%'
on conflict (telegram_id) do nothing;
