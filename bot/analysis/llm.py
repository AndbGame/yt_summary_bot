"""LLM-слой: разбор контента в JSON-отчёт с ретраями и JSON-режимом провайдера.

Ретраи перенесены из summarize() фазы 1 без изменений семантики: экспоненциальный
бэк-офф с джиттером, Retry-After провайдера приоритетнее расчётной паузы, число
попыток и кэп — из BotConfig. analyze_content() вызывается из воркера через
asyncio.to_thread, поэтому синхронный time.sleep здесь допустим (событийный
цикл не блокируется).

JSON-режим (response_format=json_object) включён по умолчанию, но не все
OpenAI-совместимые провайдеры его умеют: BadRequest с 4xx-статусом, в тексте
которого «response_format» или «json» рядом со словами режима/формата/ответа,
означает отказ режима — один раз откатываемся на обычный вызов и запоминаем
решение (_json_mode_disabled), чтобы не тратить попытки на заведомо
отвергаемый параметр. Посторонние 4xx с «json» в тексте («invalid json in
request body») откат не включают. 5xx с тем же текстом — обычный сбой
провайдера, ретраится на общих основаниях, откат не нужен.
"""

import logging
import random
import time

from openai import APIConnectionError, APITimeoutError, BadRequestError, OpenAI, RateLimitError

from bot.analysis.report import Report, ReportParseError, parse_report_json
from bot.config import DEFAULT_LLM_MAX_BACKOFF

log = logging.getLogger("yt-summary")

# Дефолты, если в prompts.yaml нет секции content_analysis (промпты не должны
# ронять бот — см. load_prompts); рабочие тексты живут в prompts.yaml
_DEFAULT_SYSTEM_PROMPT = "Ты аналитик, который разбирает видео по субтитрам и отвечает строгим JSON."
_DEFAULT_USER_TEMPLATE = "Разбери видео по субтитрам и верни JSON-отчёт.\n\nСУБТИТРЫ:\n{transcript}"


def _retry_after_seconds(exc, cap: float = DEFAULT_LLM_MAX_BACKOFF):
    """Достаёт Retry-After из ответа API (секунды), если провайдер его прислал."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
        raw = headers.get(key)
        if raw:
            try:
                return min(cap, max(1.0, float(raw)))
            except (TypeError, ValueError):
                continue
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        try:
            return min(cap, max(1.0, float(body.get("error", {}).get("retry_after"))))
        except (TypeError, ValueError):
            pass
    return None


def _is_json_mode_rejection(exc) -> bool:
    """Отказ провайдера именно в JSON-режиме: 4xx + «response_format»/«json»-режим в тексте.

    По тексту, а не по коду ошибки: единого кода «не поддерживаю response_format»
    у совместимых провайдеров нет. «json» сам по себе — слабый признак: он
    встречается в посторонних 4xx («invalid json in request body»), поэтому
    требуется слово о режиме/формате/объекте/ответе. 5xx — сбой инфраструктуры,
    откат не нужен.
    """
    text = str(exc).lower()
    if "response_format" not in text and not (
        "json" in text and any(w in text for w in ("mode", "format", "object", "response"))
    ):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


class LLMClient:
    """Разбор контента через LLM; попытки, кэп бэк-оффа и бюджет токенов — из конфига."""

    def __init__(self, config, prompts: dict):
        self.config = config
        self.prompts = prompts
        # откат JSON-режима — на время жизни клиента: ответ провайдера не меняется
        # между вызовами, повторять отвергаемый параметр бессмысленно
        self._json_mode_disabled = False

    def analyze_content(self, transcript: str) -> Report:
        """Разбор субтитров в Report. Ретраи на 429/сетевые сбои/мусорный ответ.

        ReportParseError (LLM ответил не JSON) трактуется как «пустой ответ»:
        короткая пауза и повтор. Бросает исключение только после
        config.llm_max_attempts неудач — вызывающий код тогда отправляет в тред
        текст ошибки из prompts.yaml.
        """
        client = OpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)
        section = self.prompts.get("content_analysis") or {}
        # replace вместо format: схема JSON в промпте полна фигурных скобок
        user_content = (section.get("user") or _DEFAULT_USER_TEMPLATE).replace(
            "{transcript}", transcript[: self.config.transcript_limit]
        )
        messages = [
            {"role": "system", "content": section.get("system") or _DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        last_exc = None
        attempt = 1
        while attempt <= self.config.llm_max_attempts:
            kwargs = {
                "model": self.config.llm_model,
                "messages": messages,
                "max_tokens": self.config.llm_max_tokens,
                "temperature": 0.3,
            }
            if not self._json_mode_disabled:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise RuntimeError("LLM вернул пустой ответ")
                return parse_report_json(content)
            except BadRequestError as e:
                if not self._json_mode_disabled and _is_json_mode_rejection(e):
                    self._json_mode_disabled = True
                    log.warning("LLM: провайдер отверг JSON-режим (%s), повтор без response_format", e)
                    # немедленный повтор без параметра: попытку не тратим, пауза не нужна —
                    # это не сбой, а смена контракта вызова
                    continue
                raise
            except RateLimitError as e:
                wait = _retry_after_seconds(e, cap=self.config.llm_max_backoff) or min(
                    self.config.llm_max_backoff, 5 * 2 ** (attempt - 1)
                )
                log.warning(
                    "LLM: лимит запросов (попытка %d/%d), пауза %.0fс",
                    attempt, self.config.llm_max_attempts, wait,
                )
                last_exc = e
            except (APIConnectionError, APITimeoutError) as e:
                wait = min(self.config.llm_max_backoff, 3 * 2 ** (attempt - 1))
                log.warning(
                    "LLM: сетевой сбой (попытка %d/%d), пауза %.0fс: %s",
                    attempt, self.config.llm_max_attempts, wait, e,
                )
                last_exc = e
            except (RuntimeError, ReportParseError) as e:
                # пустой ответ или мусор вместо JSON — короткая пауза и повтор
                wait = 2 * attempt
                log.warning("LLM: %s (попытка %d/%d)", e, attempt, self.config.llm_max_attempts)
                last_exc = e
            # джиттер, чтобы при пачке запросов не биться синхронно
            time.sleep(wait + random.uniform(0, 1))
            attempt += 1
        raise last_exc or RuntimeError("LLM: исчерпаны попытки")
