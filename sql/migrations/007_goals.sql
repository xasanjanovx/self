-- Migration 007: универсальные цели (не только накопления) + взвешивания + отметки по привычкам.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно (можно запускать повторно).

-- Таблица savings_goals становится общей таблицей целей. Старые строки — kind='save'.
--   kind: save      — накопить target_amount (сум) к deadline; saved_amount — отложено
--         spend_cap — тратить не больше target_amount за месяц (params.category — только категория,
--                     params.month — 'YYYY-MM' конкретный месяц или null = каждый месяц,
--                     params.baseline — обычные траты/мес, если цель была «сэкономить X»)
--         weight    — дойти до target_amount кг к deadline (params.start_weight, params.start_date)
--         habit     — target_amount раз в неделю (params.per_week), отметки в goal_checkins
--         custom    — свободная цель: target_amount=100 (%), saved_amount — прогресс в %
alter table savings_goals add column if not exists kind text not null default 'save';
alter table savings_goals add column if not exists params jsonb not null default '{}'::jsonb;
alter table savings_goals add column if not exists unit text;

create table if not exists weight_logs (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  weight numeric not null check (weight > 0),
  day date not null,
  created_at timestamptz not null default now(),
  unique (telegram_id, day)
);
create index if not exists weight_logs_telegram_idx on weight_logs (telegram_id, day desc);

create table if not exists goal_checkins (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  goal_id bigint not null references savings_goals(id) on delete cascade,
  day date not null,
  value numeric not null default 1,
  note text,
  created_at timestamptz not null default now(),
  unique (telegram_id, goal_id, day)
);
create index if not exists goal_checkins_goal_idx on goal_checkins (telegram_id, goal_id, day desc);
