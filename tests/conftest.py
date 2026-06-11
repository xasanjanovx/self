"""Pytest setup: force dummy credentials so importing the bot package never
touches real services. All tests here exercise pure functions only.
"""
import os

# Force dummy env BEFORE any bot.* import triggers config.load_settings().
os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["SUPABASE_URL"] = "https://dummy.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.sig"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"
