-- Migration 008: подъём на фаджр — настройки звонка, журнал подъёмов.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно (можно запускать повторно).

create table if not exists wake_settings (
  telegram_id bigint primary key references users(telegram_id) on delete cascade,
  enabled boolean not null default true,
  mode text not null default 'fajr',        -- fajr — за offset_min до такбира; fixed — в fixed_time
  fixed_time text,                          -- 'HH:MM' для mode='fixed'
  offset_min int not null default 25,       -- за сколько минут до такбира звонить
  takbir_offset_min int not null default 20,-- такбир = азан фаджра + это (поправка под свою мечеть)
  days_of_week int[] not null default '{1,2,3,4,5,6,7}',  -- 1=пн … 7=вс
  latitude numeric not null default 40.7821,   -- Андижан
  longitude numeric not null default 72.3442,
  calc_method int not null default 3,       -- Aladhan: 3 = Muslim World League
  call_enabled boolean not null default true,
  max_attempts int not null default 20,     -- сколько раз перезванивать
  retry_seconds int not null default 45,    -- пауза между попытками
  confirm_tasks text[] not null default '{water,squats,question,hadith}',  -- какие задания разрешены
  hardness text not null default 'normal',  -- normal | hard
  skip_until date,                          -- «не буди до …»
  updated_at timestamptz not null default now()
);

create table if not exists wake_log (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  day date not null,
  planned_at timestamptz,                   -- во сколько собирались будить
  takbir_at text,                           -- 'HH:MM' такбира этого дня
  first_call_at timestamptz,
  attempts int not null default 0,
  woke_at timestamptz,                      -- когда подтвердил подъём
  woke_source text,                         -- call | message | early | button
  task_kind text,                           -- water | squats | question | hadith
  task_text text,
  task_done_at timestamptz,
  before_takbir boolean,                    -- успел ли встать до такбира
  created_at timestamptz not null default now(),
  unique (telegram_id, day)
);
create index if not exists wake_log_telegram_idx on wake_log (telegram_id, day desc);
