create extension if not exists pgcrypto;

create table if not exists users (
  telegram_id bigint primary key,
  username text,
  first_name text,
  language text not null default 'ru',
  timezone text not null default 'Asia/Tashkent',
  currency text not null default 'UZS',
  created_at timestamptz not null default now()
);

create table if not exists goals (
  id uuid primary key default gen_random_uuid(),
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  goal_type text not null check (goal_type in ('weight', 'budget', 'habit')),
  title text not null,
  target_value numeric,
  period text not null default 'weekly',
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create index if not exists goals_telegram_idx on goals (telegram_id);

create table if not exists habits (
  id uuid primary key default gen_random_uuid(),
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  name text not null,
  target_per_week integer not null default 7,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create index if not exists habits_telegram_idx on habits (telegram_id);

create table if not exists habit_logs (
  id bigserial primary key,
  habit_id uuid not null references habits(id) on delete cascade,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  log_date date not null,
  completed boolean not null default true,
  note text,
  created_at timestamptz not null default now(),
  unique (habit_id, log_date)
);

create index if not exists habit_logs_telegram_date_idx on habit_logs (telegram_id, log_date);

create table if not exists finance_entries (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  entry_type text not null check (entry_type in ('income', 'expense')),
  amount numeric not null check (amount > 0),
  category text not null,
  note text,
  entry_date date not null default current_date,
  source text not null default 'manual',
  created_at timestamptz not null default now()
);

create index if not exists finance_entries_telegram_date_idx on finance_entries (telegram_id, entry_date);

create table if not exists daily_checkins (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  checkin_date date not null,
  mood integer check (mood between 1 and 10),
  energy integer check (energy between 1 and 10),
  weight numeric,
  note text,
  created_at timestamptz not null default now(),
  unique (telegram_id, checkin_date)
);

create index if not exists daily_checkins_telegram_date_idx on daily_checkins (telegram_id, checkin_date);

create table if not exists calorie_logs (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  photo_url text,
  meal_desc text,
  calories integer,
  protein numeric,
  fat numeric,
  carbs numeric,
  confidence numeric,
  advice text,
  created_at timestamptz not null default now()
);

create index if not exists calorie_logs_telegram_date_idx on calorie_logs (telegram_id, created_at desc);

create table if not exists reminders (
  id uuid primary key default gen_random_uuid(),
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  reminder_text text not null,
  reminder_time time not null,
  days_of_week integer[] not null default array[1,2,3,4,5,6,7],
  timezone text not null default 'Asia/Tashkent',
  enabled boolean not null default true,
  last_sent_key text,
  created_at timestamptz not null default now()
);

create index if not exists reminders_telegram_idx on reminders (telegram_id);

create table if not exists weekly_report_runs (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  iso_year integer not null,
  iso_week integer not null,
  sent_at timestamptz not null default now(),
  unique (telegram_id, iso_year, iso_week)
);

create index if not exists weekly_report_runs_telegram_idx on weekly_report_runs (telegram_id, iso_year, iso_week);
