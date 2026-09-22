-- Migration 010: Джарвис — обращение «Шеф/Сэр/Босс», утро голосом, звонки о важном,
-- ожидание фото («пришлю фото — пойми»), и журнал временных сообщений чата.
-- Выполнить в Supabase -> SQL Editor. Идемпотентно.

alter table assistant_settings
  add column if not exists honorific text not null default 'mix',        -- none | shef | ser | boss | mix
  add column if not exists morning_voice boolean not null default false,  -- утренняя сводка голосом
  add column if not exists alert_calls boolean not null default false,    -- звонить, если важное
  add column if not exists alert_call_day date,                           -- когда последний раз звонил о важном
  add column if not exists photo_intent text,                             -- что делать со следующим фото
  add column if not exists photo_intent_until timestamptz;

-- Временные сообщения бота («Звоню», «Готово», «Не дозвонился»…): удаляются по сроку
-- даже после перезапуска бота (раньше таймер жил только в памяти процесса).
create table if not exists ephemeral_messages (
  chat_id bigint not null,
  message_id bigint not null,
  delete_at timestamptz not null,
  created_at timestamptz not null default now(),
  primary key (chat_id, message_id)
);
create index if not exists ephemeral_messages_due on ephemeral_messages (delete_at);
alter table ephemeral_messages enable row level security;
