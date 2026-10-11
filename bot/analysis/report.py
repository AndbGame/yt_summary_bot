"""Модель отчёта фазы 2: блоки первого класса + JSON-схема ответа LLM.

Почему блоки — сущность, а не один текст: кеш, рендер и доставка работают с
ними по отдельности (ARCHITECTURE.md §5). Отчёт кладётся в кеш как JSON
(to_json/from_json), рендерится в список Discord-сообщений с прогрессивной
детализацией «кратко -> тезисы -> подробно» и валидируется на бюджеты символов
до отправки. Единственный источник структуры — JSON-ответ LLM по схеме из
prompts.yaml (content_analysis), поэтому from_analysis доверяет форме не больше,
чем нужно: отсутствующие/пустые части просто не дают блоков.
"""

import json
import re

# Фиксированный список видов блоков: рендер и валидация бюджетов знают ровно их
KINDS = ["brief", "context", "theses", "detailed", "author"]

# Маркер обрезки: ведущий пробел, чтобы приклеивался к тексту читаемо
TRUNCATION_MARKER = " …(обрезано)"

# Бюджет блока лежит в ключе budgets по его виду; detailed-блоков — в "section"
# (так ключ называется в prompts.yaml: «бюджет одного раздела»)
_BUDGET_KEY_BY_KIND = {
    "brief": "brief",
    "context": "context",
    "theses": "theses",
    "detailed": "section",
    "author": "author",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)

# Сколько сырья попадает в текст ReportParseError: traceback не нужен, но и
# молча терять ответ провайдера нельзя — фрагмент достаточен для диагностики
_ERROR_FRAGMENT_LIMIT = 200


class ReportParseError(ValueError):
    """Ответ LLM не удалось разобрать в JSON-отчёт; текст несёт фрагмент сырья."""


class ReportBlock:
    """Один блок отчёта: вид, заголовок, тело в Discord-markdown и данные.

    data — необязательные поля (complexity у brief, start_sec у detailed);
    mutable default не разделяется между экземплярами — каждый получает свой dict.
    """

    def __init__(self, kind: str, title: str = "", body_md: str = "", data: dict | None = None):
        self.kind = kind
        self.title = title
        self.body_md = body_md
        self.data = dict(data) if data else {}

    def __repr__(self) -> str:  # диагностика кеша/логов без __eq__
        return f"ReportBlock({self.kind!r}, title={self.title!r})"


class Report:
    """Упорядоченный список блоков; сериализация — контракт кеша отчётов."""

    def __init__(self, blocks: list[ReportBlock]):
        self.blocks = list(blocks)

    @classmethod
    def from_analysis(cls, analysis: dict) -> "Report":
        """Собирает отчёт из JSON-ответа LLM по схеме content_analysis.

        Терпеливо к пропущенным и пустым частям: их просто нет в отчёте —
        рендер не выдаёт пустых сообщений, кеш не хранит пустых блоков.
        """
        blocks: list[ReportBlock] = []

        brief = _text(analysis.get("brief"))
        if brief:
            blocks.append(
                ReportBlock(
                    "brief",
                    body_md=brief,
                    # метка сложности едет с brief: рендер решает по ней, предупреждать ли
                    data={"complexity": _text(analysis.get("complexity"))},
                )
            )

        context = _text(analysis.get("context"))
        if context:
            blocks.append(ReportBlock("context", body_md=context))

        theses = analysis.get("theses")
        if isinstance(theses, str):
            theses = [theses]
        if isinstance(theses, list):
            items = [t for t in (_text(item) for item in theses) if t]
            if items:
                blocks.append(ReportBlock("theses", body_md="\n".join(f"- {t}" for t in items)))

        sections = analysis.get("sections")
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict):
                continue
            title, body = _text(section.get("title")), _text(section.get("body"))
            if not title and not body:
                continue
            # start_sec всегда в data (None, если не определён): рендер отличает
            # «нет таймкода» от «таймкод 0» — ноль валиден, это начало видео
            blocks.append(
                ReportBlock("detailed", title=title, body_md=body, data={"start_sec": section.get("start_sec")})
            )

        return cls(blocks)

    def to_json(self) -> str:
        """Блоки в JSON-строку для кеша; ensure_ascii=False — читаемо в SQL глазами."""
        payload = {
            "blocks": [
                {"kind": b.kind, "title": b.title, "body_md": b.body_md, "data": b.data}
                for b in self.blocks
            ]
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Report":
        """Восстанавливает отчёт из to_json; форма «список или {blocks: [...]}»."""
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("blocks")
        if not isinstance(data, list):
            raise ValueError("payload кеша отчёта не содержит список блоков")
        blocks = [
            ReportBlock(
                entry["kind"],
                title=entry.get("title", ""),
                body_md=entry.get("body_md", ""),
                data=entry.get("data", {}),
            )
            for entry in data
            if isinstance(entry, dict)
        ]
        return cls(blocks)


def parse_report_json(raw: str) -> Report:
    """Терпеливый парсер сырья LLM -> Report.

    LLM любит оборачивать JSON в markdown-фенсы и пояснительный текст, поэтому
    пробуем по очереди: прямой JSON, содержимое ```-фенса, подстроку от первой
    "{" до последней "}" и raw_decode (первый валидный JSON-объект в тексте).
    Ничего не вышло — ReportParseError с фрагментом сырья (ретрай как «пустой
    ответ», а после исчерпания попыток фрагмент уходит в error-сообщение).
    """
    text = (raw or "").strip()
    candidates = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return Report.from_analysis(parsed)

    # последний шанс: строка может начинаться мусором перед валидным JSON,
    # который first-{...last-} не вырезал целиком
    if start >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return Report.from_analysis(parsed)

    raise ReportParseError(
        f"ответ LLM не является валидным JSON-отчётом "
        f"(фрагмент сырья: {text[:_ERROR_FRAGMENT_LIMIT]})"
    )


def validate_budgets(report: Report, budgets: dict | None) -> list[str]:
    """Человекочитаемые предупреждения о превышении бюджетов символов.

    Валидация — вторая линия обороны после промпта: превышение не роняет
    доставку (рендер укорачивает brief/context/theses), но фиксируется в логе.
    Нет ключа бюджета — нечего превышать.
    """
    if not budgets:
        return []
    warnings = []
    for block in report.blocks:
        key = _BUDGET_KEY_BY_KIND.get(block.kind)
        limit = budgets.get(key) if key else None
        if limit is None:
            continue
        size = len(block.body_md)
        if size > limit:
            label = f"block '{block.kind}'"
            if block.kind == "detailed" and block.title:
                label += f" ({block.title})"
            warnings.append(f"{label}: {size} > {limit} символов")
    return warnings


def render_report_messages(
    report: Report, url: str, budgets: dict | None = None, section_budget: int = 1900
) -> list[str]:
    """Прогрессивный рендер: кратко (+контекст) -> тезисы -> разделы -> автор.

    Правила границ ответственности:
    - заголовок разбора «**Разбор: Название (Автор)**» добавляет pipeline
      (он знает метаданные джобы), не рендер;
    - detailed-блоки УПАКОВЫВАЮТСЯ в как можно меньше сообщений (лимит
      section_budget на сообщение): один пост на тайминг — шум, читатель
      хочет компактный разбор; одиночное длинное сообщение дальше режет
      чанк-сплиттер пайплайна с fence-восстановлением;
    - brief/context/theses укорачиваются до бюджетов с маркером — это уже
      финальный вид, чанк-сплиттеру резать их не потребуется;
    - автор — последний независимо от позиции блока: финальный контекст;
    - ссылки таймкодов маскируются <url>: иначе Discord рисует на каждый
      тайминг превью-встройку видео.
    """
    brief = next((b for b in report.blocks if b.kind == "brief"), None)
    context = next((b for b in report.blocks if b.kind == "context"), None)
    messages: list[str] = []

    head = []
    if brief is not None:
        head.append(f"**Кратко**\n\n{_fit(brief.body_md, budgets, 'brief')}")
        if brief.data.get("complexity") == "specialized":
            head.append("⚠️ Узкоспециальная тема.")
    if context is not None:
        head.append(f"> *Простыми словами:* {_fit(context.body_md, budgets, 'context')}")
    if head:
        messages.append("\n\n".join(head))

    theses = next((b for b in report.blocks if b.kind == "theses"), None)
    if theses is not None:
        messages.append(f"**Тезисы**\n\n{_fit(theses.body_md, budgets, 'theses')}")

    packed: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for block in report.blocks:
        if block.kind != "detailed":
            continue
        link = _time_link(block.data.get("start_sec"), url)
        body = f"{link}\n\n{block.body_md}" if link else block.body_md
        part = f"**{block.title}**\n\n{body}"
        # грубая упаковка по символам: точную подгонку всё равно делает
        # чанк-сплиттер, здесь важно не плодить сообщения
        if buf and buf_len + len(part) + 2 > section_budget:
            packed.append("\n\n".join(buf))
            buf, buf_len = [], 0
        buf.append(part)
        buf_len += len(part) + 2
    if buf:
        packed.append("\n\n".join(buf))
    messages.extend(packed)

    author = next((b for b in report.blocks if b.kind == "author"), None)
    if author is not None:
        messages.append(f"**Обзор автора**\n\n{_fit(author.body_md, budgets, 'author')}")

    return messages


def _text(value) -> str:
    """Строка из чего угодно: None -> пусто, остальное -> str (терпимость к LLM)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _fit(body: str, budgets: dict | None, budget_key: str) -> str:
    """Укорачивает тело до бюджета блока с маркером обрезки (без бюджета — как есть)."""
    limit = (budgets or {}).get(budget_key)
    if limit is not None and len(body) > limit:
        return body[:limit] + TRUNCATION_MARKER
    return body


def _time_link(start_sec, url: str) -> str | None:
    """Таймкод -> кликабельная метка [mm:ss](<url?t=<sec>>); 0 секунд — валиден.

    URL обёрнут в <…>: без этого Discord на каждый тайминг рисует превью-встройку
    видео — разбор превращается в ленту карточек.
    """
    try:
        sec = int(start_sec)
    except (TypeError, ValueError):
        return None
    if sec < 0:
        return None
    minutes, seconds = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    return f"[{label}](<{url}?t={sec}>)"
