-- Migration 002: persist the "live screen" message id per user so the single
-- menu survives bot restarts (no more duplicate menus accumulating).
--
-- HOW TO APPLY: open Supabase -> SQL Editor, run this file. Safe & idempotent.
-- The bot auto-detects the column; if it's missing it just keeps the screen id
-- in memory (menus can still duplicate after a restart until you run this).

alter table users add column if not exists screen_message_id bigint;
