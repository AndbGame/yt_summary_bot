"""Версия промптов — обязательная часть ключа кеша отчётов (фаза 1, ARCHITECTURE.md §6).

prompts.yaml обязан нести поле version: смена промптов поднимает версию и
детерминированно инвалидирует старые отчёты — ручной сброс кеша не нужен.
"""

from bot.analysis.prompts import load_prompts


def test_prompts_yaml_declares_version():
    prompts = load_prompts()
    assert "version" in prompts, "prompts.yaml должен объявлять version — она входит в ключ кеша"
    # pipeline приводит к str(deps.prompts.get("version", "0")); в yaml — "1"
    assert str(prompts["version"]) == "1"
