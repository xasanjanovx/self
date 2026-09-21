-- Migration 005: ассистент — заметки, задачи, цели накоплений, сроки долгов,
-- журнал проактивных уведомлений, переключатели «подсказки» и «голосовые ответы».
-- Выполнить в Supabase -> SQL Editor. Идемпотентно (можно запускать повторно).

create table if not exists notes (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  text text not null,
  created_at timestamptz not null default now()
);
create index if not exists notes_telegram_idx on notes (telegram_id, created_at desc);

create table if not exists tasks (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  text text not null,
  due_date date,
  due_time text,               -- 'HH:MM' локального времени; null = в течение дня
  done boolean not null default false,
  done_at timestamptz,
  notified_key text,           -- чтобы напоминать о задаче со временем один раз
  created_at timestamptz not null default now()
);
create index if not exists tasks_telegram_idx on tasks (telegram_id, done, due_date);

create table if not exists savings_goals (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  title text not null,
  target_amount numeric not null check (target_amount > 0),
  saved_amount numeric not null default 0,
  deadline date,
  done boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists savings_goals_telegram_idx on savings_goals (telegram_id, done);

create table if not exists debt_deadlines (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  person text not null,        -- имя как в комментарии операции
  side text not null check (side in ('lent', 'debt')),
  due_date date not null,
  note text,
  created_at timestamptz not null default now(),
  unique (telegram_id, person, side)
);

create table if not exists alerts_log (
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  key text not null,
  sent_at timestamptz not null default now(),
  primary key (telegram_id, key)
);

alter table user_settings add column if not exists proactive boolean not null default true;
alter table user_settings add column if not exists voice_reply boolean not null default true;
