"""Чистота импорта: пакет bot импортируется без токенов, без .env и без создания клиента.

Критично для фазы 0: монолит падал/читал окружение на импорте — пакет обязан
не иметь side effects до явного вызова main()/build_deps().
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import discord
import pytest

ROOT = Path(__file__).resolve().parents[2]

# что не копируем при сборке изолированной копии репозитория
_COPY_IGNORE = shutil.ignore_patterns(
    ".env", ".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"
)

_CHECK_SCRIPT = (
    "import bot, bot.config, bot.main; "
    "import discord; "
    "bad = [f'{m.__name__}.{n}' "
    "for m in (bot, bot.config, bot.main) for n, v in vars(m).items() "
    "if isinstance(v, discord.Client)]; "
    "raise SystemExit(f'создан discord.Client на уровне модуля: {bad}' if bad else 0)"
)


def test_cold_import_without_tokens_and_env_file(tmp_path, monkeypatch):
    """Холодный импорт в подпроцессе: без токенов в окружении и без .env вовсе.

    Копируем репозиторий во временную папку без .env — проверяем, что импорт
    не требует ни переменных, ни файла конфигурации и не создаёт discord.Client.
    """
    copy_root = tmp_path / "repo"
    shutil.copytree(ROOT, copy_root, ignore=_COPY_IGNORE)

    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_BOT_TOKEN", "LLM_API_KEY")}
    env["PYTHONPATH"] = str(copy_root)

    proc = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        env=env,
        cwd=str(copy_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"


def test_no_discord_client_created_on_import(monkeypatch, tmp_path):
    """Импорт без токенов не создаёт discord.Client на уровне модулей пакета."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # импорт не должен зависеть и от cwd

    import bot
    import bot.config  # noqa: F401
    import bot.discord_app  # noqa: F401
    import bot.main  # noqa: F401

    for mod in (bot, bot.config, bot.discord_app, bot.main):
        for name, value in vars(mod).items():
            if isinstance(value, discord.Client):
                pytest.fail(f"{mod.__name__}.{name}: discord.Client создан при импорте")
