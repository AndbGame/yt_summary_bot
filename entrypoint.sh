#!/bin/sh
# При каждом старте контейнера: подтянуть код из GitHub и обновить зависимости.

if command -v git >/dev/null 2>&1 && [ -d /app/.git ]; then
    echo "[entrypoint] git pull..."
    git config --global --add safe.directory /app
    git -C /app fetch origin
    git -C /app reset --hard origin/main 2>/dev/null || git -C /app reset --hard origin/master
    echo "[entrypoint] код обновлён"
else
    echo "[entrypoint] git недоступен или .git нет — работаю на текущей копии кода"
fi

if [ -f /app/requirements.txt ]; then
    echo "[entrypoint] pip install -r requirements.txt..."
    pip install --no-cache-dir -q -r /app/requirements.txt || echo "[entrypoint] pip update failed, работаю на встроенных зависимостях"
fi

echo "[entrypoint] запуск бота..."
exec python /app/yt_summary_bot.py
