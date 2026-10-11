# Стратегия тестирования

Принцип: **в CI нет сети**. Реальные Discord/LLM/YouTube — только опциональный
live-smoke вручную. Всё остальное — быстрые детерминированные тесты на фейках.

## Пирамида

```
        ┌─────────────┐   live smoke (маркер `live`, вручную, реальные токены)
        ├─────────────┤   e2e in-process: полный пайплайн на фейках (tests/e2e/)
        ├─────────────┤   компонентные: экстракторы на записанных fixture'ах,
        │             │   LLM-слой на FakeLLM, репозитории на tmp-SQLite
        └─────────────┘   юнит: чистые функции (регексы, сплиттер, ретраи…)
```

## Как запускать

```bash
.venv/Scripts/python.exe -m pytest                  # всё, кроме live
.venv/Scripts/python.exe -m pytest tests/unit       # только юнит
.venv/Scripts/python.exe -m pytest -m live          # только live (нужны токены)
.venv/Scripts/python.exe -m ruff check bot tests yt_summary_bot.py
```

## Что уже покрыто (карта тест → модуль)

После фазы 0 (монолит расщеплён в пакет `bot/`, тесты перенесены 1:1 + расширены):

| Файл | Проверяет | Модуль |
|---|---|---|
| `tests/unit/test_config.py` | дефолты, .env-чтение, приоритет env, мусорные int → дефолт, missing_fields | `bot/config.py` |
| `tests/unit/test_render.py` | таблицы→буллеты (в т.ч. в code-fence), fence-aware сплит: 2000, `(i/N)`, переоткрытие фенса | `bot/delivery/render.py` |
| `tests/unit/test_llm.py` | `Retry-After` (кэп из конфига), ретраи 429/сети, параметры API-вызова из конфига, обрезка субтитров | `bot/analysis/llm.py` |
| `tests/unit/test_outbox.py` | JSON-буфер: unicode, битый файл, атомарная запись | `bot/delivery/outbox.py` |
| `tests/unit/test_sender.py` | safe_send ретраи/4xx без ретрая, flush: порядок/мёртвые треды/стоп-на-сбое, flush_loop | `bot/delivery/sender.py` |
| `tests/unit/test_youtube.py` | YT-регекс (все форматы, ложные срабатывания), имя треда | `bot/sources/youtube.py` |
| `tests/unit/test_jobs.py` | очередь, живучесть воркера при исключениях | `bot/jobs.py` |
| `tests/unit/test_import_clean.py` | импорт `bot.*` без side effects (холодный импорт в подпроцессе без токенов/.env) | весь пакет |
| `tests/e2e/test_pipeline.py` | полный путь job: чанки/формат, ошибка → сообщение, офлайн → буфер → досылка, error-путь без диска | `bot/pipeline.py` |
| `tests/e2e/test_handle.py` | роутинг ссылки, дедуп, 160004, Forbidden, позиция в очереди | `bot/discord_app.py` |
| `tests/e2e/test_discord_app.py` | on_ready: однократный старт воркеров, flush при старте | `bot/discord_app.py` |
| `tests/e2e/test_main.py` | build_deps (реальная композиция), main(): валидация/env-имена, запуск | `bot/main.py` |
| `tests/unit/test_store.py` | SQLite: схема reports/outbox, WAL, идемпотентные миграции, FIFO, параллельные записи, close/reopen | `bot/store/` |
| `tests/unit/test_store_outbox_compat.py` | общий набор outbox-поведений на PendingOutbox (JSON) и DbOutbox (SQLite) — LSP-совместимость | `bot/delivery/outbox.py`, `bot/store/outbox.py` |
| `tests/unit/test_canonical.py` | формат canonical_id | `bot/sources/youtube.py` |
| `tests/e2e/test_pipeline_cache.py` | кеш: промах/хит/перезапуск/смена версии промптов, ошибки не кешируются | `bot/pipeline.py`, `bot/store/reports.py` |
| `tests/e2e/test_legacy_migration.py` | миграция pending_replies.json → БД: атомарный merge одной транзакцией, битые/кривые файлы → .corrupted | `bot/main.py` |
| `tests/unit/test_report.py` | Report/ReportBlock: from_analysis, JSON roundtrip, терпеливый парсер, бюджеты, прогрессивный рендер (таймкоды, context, автор последним) | `bot/analysis/report.py` |
| `tests/e2e/test_report_flow.py` | полный путь JSON-отчёта в тред, обрезка по бюджетам, мусор от LLM → error-промпт | `bot/pipeline.py`, `bot/analysis/report.py` |

## E2E in-process: как устроено

«Ходячий скелет»: `process_job()` / `handle()` / `flush_pending()` гоняются
через `asyncio.run()` с подменёнными краями:

- `FakeThread` / `FakeStatus` / `FakeMessage` — Discord-объекты, пишут в списки;
- `fetch_transcript` / `summarize` / `_resolve_channel` — monkeypatch на модуль.

Проверяется **пользовательски видимое**: какие сообщения, в каком порядке,
какой длины, с какой разметкой. Сегодня monkeypatch глобалов — вынужденный
шаг (модуль монолитен); фаза 0 архитектуры заменяет его на явные порты,
и фейки переезжают в `conftest.py` как фикстуры.

Ключевые e2e-сценарии (расширяются по фазам миграции):

| Сценарий | Ожидание |
|---|---|
| длинный саммари с таблицей и кодом | чанки ≤1900, таблицы сконвертированы, фенсы целы, `(i/N)` |
| нет субтитров / LLM упала | одно сообщение об ошибке из `prompts.yaml`, статус удалён |
| Discord 5xx при отправке | чанки ушли в буфер; после «восстановления» — досылка по порядку |
| тред удалён | сообщение отброшено, буфер не растёт вечно |
| повторное сообщение с той же ссылкой | одна задача в очереди |
| (фаза 1) повторная ссылка на разобранное видео | ответ из кеша, ноль LLM-вызовов |
| (фаза 2+) отчёт из блоков | сообщение 1 = кратко+тезисы, далее подробно, в конце автор |

## Компонентные тесты (по мере миграции)

- **Экстракторы на fixture'ах**: реальные HTML/страницы API однажды
  сохраняются в `tests/fixtures/` (запись — отдельный скриптом с сетью,
  воспроизведение — без). Контракт: `extract(url) -> SourceDocument`
  с ожидаемыми полями. Защищает от поломок при рефакторинге портов.
- **FakeLLM**: один класс, сценарии «429→успех», «пусто», «битый JSON»,
  «превышен бюджет блока». Для фазы 2 (JSON-схема отчёта) — property-тесты
  валидатора: любой осмысленный ответ модели либо парсится, либо даёт
  понятную ошибку.
- **Репозитории**: SQLite во временном файле; миграции применяются на пустой
  БД при каждом прогоне.

## Live smoke (вручную, `-m live`)

Цель — убедиться, что интеграции живы (токены, права, лимиты, сеть),
а не проверить логику. Чек-лист сценария на тестовом сервере:

1. бот поднялся (`on_ready` в логах, нет ошибок интентов);
2. пост YouTube-ссылки → тред создан, статус → саммари в разумное время;
3. видео без субтитров → внятное сообщение об ошибке;
4. повторный пост той же ссылки → (после фазы 1) мгновенный ответ из кеша;
5. `docker compose restart` во время обработки → задача не потерялась
   (после фазы 4), зависших статусов нет.

В CI не запускается; токены — из секретов окружения, не из коммита.

## CI (GitHub Actions, предложить)

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements-dev.txt
      - run: ruff check yt_summary_bot.py tests/
      - run: pytest   # live исключён дефолтным addopts/маркерами
```

## Чек-лист для новых изменений

- новая ветка логики → юнит-тест на чистой функции;
- новый источник/порт → компонентный тест на fixture + строка в реестр;
- изменение формата сообщений → e2e-сценарий обновлён (пользовательский вид!);
- изменение промптов → версия промпта поднята (инвалидация кеша, фаза 1+);
- изменение надёжности (ретраи/буфер) → тест на сбойный путь обязателен.
