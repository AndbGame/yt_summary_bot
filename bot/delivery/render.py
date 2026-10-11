"""Discord-форматирование LLM-ответов (перенос из монолита, изначально из Hermes).

Портировано из gateway/platforms/helpers.py и base.py агента Hermes:
  convert_table_to_bullets / _render_table_block — GFM-таблицы не рендерятся
  в Discord, превращаем их в "**заголовок** + • поле: значение";
  truncate_message — сплит длинных сообщений с закрытием/переоткрытием
  code-fence на границах и индикаторами (1/N).
"""

import re

# Discord-лимит сообщения (как в адаптере Hermes: 2000 лимит, сплит на 1900, макс 8 чанков)
MAX_MESSAGE_LENGTH = 2000

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*\|.*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _split_table_row(line: str) -> list:
    line = line.strip().strip("|")
    return [c.strip() for c in line.split("|")]


def _render_table_block(table_block: list) -> str:
    headers = _split_table_row(table_block[0]) if len(table_block) >= 3 else []
    if len(headers) < 2:
        return "\n".join(table_block)
    groups = []
    for index, row in enumerate(table_block[2:], start=1):
        cells = _split_table_row(row)
        heading = next((c for c in cells if c), f"Строка {index}")
        data_cells = (cells + [""] * len(headers))[: len(headers)]
        bullets = [
            f"• {h}: {v}"
            for h, v in zip(headers, data_cells, strict=False)
            if v != heading
        ]
        groups.append("\n".join([f"**{heading}**", *bullets]))
    return "\n\n".join(groups)


def convert_table_to_bullets(text: str) -> str:
    """GFM pipe-таблицы -> жирный заголовок + буллеты; fenced код не трогаем."""
    if "|" not in text or "-" not in text:
        return text
    lines = text.split("\n")
    out, in_fence, i = [], False, 0
    while i < len(lines):
        line = lines[i]
        is_fence = line.lstrip().startswith("```")
        in_fence ^= is_fence
        if not (in_fence or is_fence) and "|" in line and i + 1 < len(lines) \
                and _TABLE_SEP_RE.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                j += 1
            out.append(_render_table_block(lines[i:j]))
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def format_for_discord(content: str) -> str:
    if not content:
        return content
    return convert_table_to_bullets(content)


def _fence_state_after(text: str, in_code: bool, lang: str):
    """(in_code, lang) после прохода по тексту; маркер языка берём с открывающего фенса."""
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_code:
                in_code, lang = True, stripped[3:].strip()
            else:
                in_code = False
    return in_code, lang


def truncate_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Сплит с сохранением code-fence: разрез внутри блока закрывает фенс в конце
    чанка и переоткрывает его (с тем же языком) в следующем; (1/N) индикаторы."""
    if len(content) <= max_length:
        return [content]
    INDICATOR_RESERVE = 10
    FENCE_CLOSE = "\n```"
    chunks, remaining, carry_lang = [], content, None
    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        headroom = max(1, max_length - INDICATOR_RESERVE - len(prefix) - len(FENCE_CLOSE))
        if len(prefix) + len(remaining) <= max_length - INDICATOR_RESERVE:
            final_chunk = prefix + remaining
            if carry_lang is not None and _fence_state_after(remaining, True, carry_lang)[0]:
                final_chunk += FENCE_CLOSE
            chunks.append(final_chunk)
            break
        region = remaining[:headroom]
        split_at = region.rfind("\n")
        if split_at < headroom // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = headroom
        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body
        in_code, lang = _fence_state_after(chunk_body, carry_lang is not None, carry_lang or "")
        carry_lang = lang if in_code else None
        chunks.append(full_chunk + FENCE_CLOSE if in_code else full_chunk)
    if len(chunks) > 1:
        chunks = [f"{c} ({i + 1}/{len(chunks)})" for i, c in enumerate(chunks)]
    return chunks
