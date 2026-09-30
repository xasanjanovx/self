-- Migration 012: задачи, созданные ботом сами (напоминание «вернуть долг» за день до срока).
-- Выполнить в Supabase -> SQL Editor. Идемпотентно.

-- Ключ автозадачи: debt:<кредитор>:<срок>. Он же не даёт создать одну задачу дважды
-- и «помнит» закрытую/удалённую пользователем — заново её не создаём.
alter table tasks add column if not exists ref_key text;
create unique index if not exists tasks_ref_key_idx on tasks (telegram_id, ref_key) where ref_key is not null;
