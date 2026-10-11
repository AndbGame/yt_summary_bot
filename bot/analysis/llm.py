"""LLM-слой: клиент OpenAI-совместимого API с ретраями на 429 и сетевых сбоях.

Перенесено из монолита 1:1: экспоненциальный бэк-офф с джиттером, Retry-After
провайдера приоритетнее расчётной паузы, число попыток и кэп — из BotConfig.
summarize() вызывается из воркера через asyncio.to_thread, поэтому синхронный
time.sleep здесь допустим (событийный цикл не блокируется).
"""

import logging
import random
import time

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from bot.config import DEFAULT_LLM_MAX_BACKOFF

log = logging.getLogger("yt-summary")

# Дефолтный system-промпт, если в prompts.yaml его нет (как в монолите)
_DEFAULT_SYSTEM_PROMPT = "Ты кратко и по делу пересказываешь видео по субтитрам."


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


class LLMClient:
    """Саммари через LLM; попытки, кэп бэк-оффа и бюджет токенов — из конфига."""

    def __init__(self, config, prompts: dict):
        self.config = config
        self.prompts = prompts

    def summarize(self, transcript: str) -> str:
        """Саммари через LLM. Ретраи на 429/лимиты и сетевые сбои, пауза экспоненциальная.

        Бросает исключение только после config.llm_max_attempts неудач — вызывающий
        код тогда отправляет в тред текст ошибки из prompts.yaml.
        """
        client = OpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)
        messages = [
            {
                "role": "system",
                "content": self.prompts.get("system", _DEFAULT_SYSTEM_PROMPT),
            },
            {
                "role": "user",
                "content": self.prompts.get("user", "").format(
                    transcript=transcript[: self.config.transcript_limit]
                ),
            },
        ]
        last_exc = None
        for attempt in range(1, self.config.llm_max_attempts + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.config.llm_model,
                    messages=messages,
                    max_tokens=self.config.llm_max_tokens,
                    temperature=0.3,
                )
                content = (resp.choices[0].message.content or "").strip()
                if content:
                    return content
                raise RuntimeError("LLM вернул пустой ответ")
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
            except RuntimeError as e:
                # пустой ответ — короткая пауза и повтор
                wait = 2 * attempt
                log.warning("LLM: %s (попытка %d/%d)", e, attempt, self.config.llm_max_attempts)
                last_exc = e
            # джиттер, чтобы при пачке запросов не биться синхронно
            time.sleep(wait + random.uniform(0, 1))
        raise last_exc or RuntimeError("LLM: исчерпаны попытки")
