"""Структура prompts.yaml фазы 2-3 — версия входит в ключ кеша отчётов.

Контракт (ARCHITECTURE.md §6, §8): version поднимается при смене промптов и
детерминированно инвалидирует старые отчёты; content_analysis несёт system/user
для JSON-разбора контента ({transcript} в user); author_review (фаза 3) —
system/user для обзора автора по фактам (в user — подстановки фактов);
budgets — бюджеты символов блоков для валидации и рендера. Тексты промптов
пишет реализователь — тесты проверяют только наличие и форму.
"""

import re

from bot.analysis.prompts import load_prompts


def test_prompts_yaml_declares_version():
    prompts = load_prompts()
    assert "version" in prompts, "prompts.yaml должен объявлять version — она входит в ключ кеша"
    # фаза 2: JSON-схема ответа LLM -> версия поднята до 2
    assert str(prompts["version"]) == "2"


def test_content_analysis_section_present():
    ca = load_prompts().get("content_analysis")
    assert isinstance(ca, dict), "prompts.yaml должен содержать секцию content_analysis"
    assert str(ca.get("system", "")).strip(), "content_analysis.system непустой"
    assert str(ca.get("user", "")).strip(), "content_analysis.user непустой"
    assert "{transcript}" in ca["user"], "user-промпт содержит подстановку {transcript}"


def test_author_review_section_present():
    # фаза 3: обзор автора строится по фактам (AuthorFacts), не по знаниям модели
    ar = load_prompts().get("author_review")
    assert isinstance(ar, dict), "prompts.yaml должен содержать секцию author_review"
    assert str(ar.get("system", "")).strip(), "author_review.system непустой"
    assert str(ar.get("user", "")).strip(), "author_review.user непустой"
    assert re.search(r"\{\w+\}", ar["user"]), (
        "user-промпт содержит подстановку фактов автора ({...}) — какие именно имена "
        "подстановок выбрать, решает реализователь, дословно не фиксируем"
    )


def test_budgets_section_with_five_keys():
    budgets = load_prompts().get("budgets")
    assert isinstance(budgets, dict), "prompts.yaml должен содержать budgets"
    assert {"brief", "context", "theses", "section", "author"} <= set(budgets)
    assert all(isinstance(v, int) and v > 0 for v in budgets.values())


def test_legacy_top_level_prompts_removed():
    prompts = load_prompts()
    assert "system" not in prompts, "старый system заменён на content_analysis.system"
    assert "user" not in prompts, "старый user заменён на content_analysis.user"


def test_error_template_kept():
    error = load_prompts().get("error")
    assert isinstance(error, str) and "{reason}" in error, "error-шаблон с {reason} остаётся"
