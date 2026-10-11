"""Загрузка промптов из prompts.yaml (формат и подстановка {transcript} — см. AGENTS.md)."""

from pathlib import Path

import yaml

# prompts.yaml живёт в корне репозитория: bot/analysis/prompts.py -> parents[2]
_DEFAULT_PROMPTS_FILE = Path(__file__).resolve().parents[2] / "prompts.yaml"


def load_prompts(path=None) -> dict:
    """Читает prompts.yaml; отсутствующий файл — не ошибка, а пустой словарь.

    Как в монолите: у мест использования есть свои дефолты (system-промпт,
    текст ошибки), поэтому пустые промпты не должны ронять бот.
    """
    p = Path(path) if path is not None else _DEFAULT_PROMPTS_FILE
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}
