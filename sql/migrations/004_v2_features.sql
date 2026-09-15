-- Migration 004: утренние/вечерние сводки, лимиты по категориям, регулярные платежи.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно (можно запускать повторно).

create table if not exists user_settings (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  brief_morning boolean not null default true,
  brief_evening boolean not null default true,
  brief_morning_time text not null default '08:00',
  brief_evening_time text not null default '21:00',
  last_morning_key text,
  last_evening_key text,
  updated_at timestamptz not null default now()
);

create table if not exists budgets (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  category text not null,
  monthly_limit numeric not null check (monthly_limit > 0),
  created_at timestamptz not null default now(),
  unique (telegram_id, category)
);

create table if not exists recurring_payments (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  title text not null,
  amount numeric not null check (amount > 0),
  category text not null default 'home',
  bucket text not null default 'card',
  day_of_month integer not null check (day_of_month between 1 and 31),
  enabled boolean not null default true,
  last_done_key text,   -- 'YYYY-MM': месяц, за который платёж уже записан или пропущен
  last_asked_key text,  -- 'YYYY-MM': месяц, за который бот уже спрашивал
  created_at timestamptz not null default now()
);

create index if not exists budgets_telegram_idx on budgets (telegram_id);
create index if not exists recurring_payments_telegram_idx on recurring_payments (telegram_id);
