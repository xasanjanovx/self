-- Migration 006: долгая память «Джарвиса» и журнал его ходов.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно.

-- Факты о пользователе (люди, привычки, даты, суммы) + дайджест последних реплик за прошлые дни.
create table if not exists user_memory (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  facts text not null default '',      -- по одной строке на факт
  recent text not null default '',     -- «дата · я: … → бот: …», последние ~20 строк
  updated_at timestamptz not null default now()
);

-- Журнал ходов агента: что спросили, какие инструменты, понял ли. Для еженедельного разбора.
create table if not exists agent_log (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  text text not null,
  kind text not null default 'agent',  -- agent | asked | unparsed_finance | unparsed_food | error
  tools text,                          -- имена инструментов через запятую
  reply text,
  ok boolean not null default true,
  created_at timestamptz not null default now()
);
create index if not exists agent_log_telegram_idx on agent_log (telegram_id, created_at desc);
