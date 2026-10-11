#!/usr/bin/env python3
"""Discord bot: создаёт тред под каждым сообщением с YouTube-ссылкой
и постит туда тезисное саммари видео (по субтитрам + LLM).

Реализация — в пакете bot/ (миграция монолита, см. docs/ARCHITECTURE.md);
этот файл остаётся точкой входа: `python yt_summary_bot.py` (в том числе
в Docker: `python /app/yt_summary_bot.py`). Настройка — через переменные
окружения или .env рядом с этим файлом (список имён — в bot/config.py).
"""

from bot.main import main

if __name__ == "__main__":
    main()
