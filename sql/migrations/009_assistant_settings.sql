-- Migration 009: настройки «Джарвиса» — голос, язык разговора и характер.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно.

create table if not exists assistant_settings (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  voice text not null default 'Sulafat',     -- голос Gemini (женский тёплый по умолчанию)
  lang text not null default 'uz',           -- язык разговора в звонках: uz | ru
  address text not null default 'sen',       -- обращение: sen («ты») | siz («вы»)
  tone text not null default 'friendly',     -- friendly | calm | strict
  verbosity text not null default 'short',   -- short | normal | detailed
  call_name text,                            -- как обращаться по имени (по умолчанию имя из Telegram)
  updated_at timestamptz not null default now()
);
