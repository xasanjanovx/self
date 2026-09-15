-- Migration 003: категории финансов теперь хранятся как ключи справочника
-- (food, groceries, transport, shopping, home, telecom, health, clothes, fun,
--  education, gifts, debt, other | salary, side, gift_in, debt_in, other_in | transfer).
-- Бот приводит старые свободные названия к ключам на лету, но для чистоты
-- данных можно выполнить этот скрипт в Supabase SQL Editor. Идемпотентен.

update finance_entries set category = 'transfer' where note like '[x:%' and category <> 'transfer';
update finance_entries set category = 'food'      where entry_type = 'expense' and lower(category) in ('еда', 'кафе', 'обед', 'ужин', 'завтрак', 'ovqat');
update finance_entries set category = 'groceries' where entry_type = 'expense' and lower(category) in ('продукты', 'oziq-ovqat');
update finance_entries set category = 'transport' where entry_type = 'expense' and lower(category) in ('транспорт', 'такси', 'taksi', 'transport');
update finance_entries set category = 'other_in'  where entry_type = 'income'  and lower(category) in ('доход', 'kirim', 'прочее');
update finance_entries set category = 'salary'    where entry_type = 'income'  and lower(category) in ('зарплата', 'oylik', 'maosh');

create index if not exists finance_entries_telegram_created_idx on finance_entries (telegram_id, created_at desc);
