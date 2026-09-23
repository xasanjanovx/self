-- Migration 011: доступ по приглашению и английский интерфейс.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно.

-- Кто, кроме владельца (ALLOWED_TELEGRAM_IDS в .env), может пользоваться ботом. Данные у каждого свои.
create table if not exists bot_members (
  telegram_id bigint primary key,
  first_name text,
  username text,
  invited_by bigint,
  created_at timestamptz not null default now()
);

-- Одноразовые ссылки-приглашения t.me/<бот>?start=inv_<code>
create table if not exists bot_invites (
  code text primary key,
  created_by bigint not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_by bigint,
  used_at timestamptz
);

-- Память переводов интерфейса ru → en (строка с числами-заглушками → перевод), чтобы каждая
-- фраза переводилась один раз, а экраны на английском открывались так же быстро.
create table if not exists ui_translations (
  src_hash text primary key,
  src text not null,
  lang text not null default 'en',
  dst text not null,
  created_at timestamptz not null default now()
);

alter table bot_members enable row level security;
alter table bot_invites enable row level security;
alter table ui_translations enable row level security;
