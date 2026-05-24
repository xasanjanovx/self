-- Migration 0002: бейджи для серий привычек.
-- Применяется руками в Supabase SQL Editor.
-- Безопасна для повторного выполнения (IF NOT EXISTS).
--
-- ВАЖНО: если у тебя в .env DB_TABLE_PREFIX=bot_ (или другой) —
-- замени `badges` ниже на `bot_badges` и `habits` на `bot_habits` соответственно.

create table if not exists badges (
  id bigserial primary key,
  telegram_id bigint not null references users(telegram_id) on delete cascade,
  badge_key text not null,           -- например: streak_habit:UUID:7
  category text not null,            -- 'habit' | 'finance' | 'nutrition' | ...
  ref_id text,                       -- ID объекта (например habit_id) или null
  threshold integer not null,        -- порог в днях/единицах
  unlocked_at timestamptz not null default now(),
  unique (telegram_id, badge_key)
);

create index if not exists badges_telegram_idx on badges (telegram_id);
create index if not exists badges_category_idx on badges (telegram_id, category);
