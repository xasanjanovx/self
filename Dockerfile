FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    TZ=Asia/Tashkent

WORKDIR /app

# matplotlib runtime deps (freetype + libpng); ffmpeg — голосовые ответы (PCM → OGG/Opus)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 \
    libpng16-16 \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-caller.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Звонки в Telegram (аккаунт-помощник). Если колёса ntgcalls недоступны для платформы —
# образ всё равно собирается, бот просто будит сообщениями (bot/caller.py это переживает).
RUN pip install --no-cache-dir -r requirements-caller.txt || echo "caller deps skipped"

COPY . .
CMD ["python", "-m", "bot.main"]
