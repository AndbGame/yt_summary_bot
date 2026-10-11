"""Источник «статья» (фаза 5): fallback-экстрактор за портом ExtractorPort.

ArticleExtractor — ПОСЛЕДНИЙ в реестре роутера (bot/main.build_deps): find
находит ЛЮБОЙ http(s)-URL, поэтому «кто обработает ссылку» решает только
порядок реестра — YouTube-ссылку первым забирает YoutubeExtractor, всё
остальное достаётся сюда. Регистронезависимость схемы в find — пин тестов:
«HTTP://» по RFC — тоже ссылка, и canonical обязан её нормализовать.

canonical_id -> «article:<norm>» — ключ кеша отчётов и дедупа. Scheme
приводится к https, потому что одна статья по http и https — один источник
(зеркала и принудительные редиректы не должны удваивать разборы); host —
в нижний регистр без userinfo (учётные данные в ключе кеша не нужны),
fragment срезается (якорь не меняет документ), tracking-параметры
(utm_*, fbclid, gclid, yclid, mc_cid, mc_eid) удаляются, пустой «?» убирается,
хвостовой «/» пути убирается — кроме корня «/», иначе canonical главной
страницы выглядел бы как хост без пути. Params пути (;jsessionid=...) — часть
path и в canonical сохраняются: страница с сессионной меткой — тот же источник.

find срезает с хвоста ссылки пунктуацию предложения («<url>», «url.», «url)»,
кавычки): Router получит чистый адрес; закрывающие скобки срезаются только
без парной открывающей внутри URL — «wiki/X_(язык)» остаётся целиком.

extract качает страницу инжектируемым fetch(url, timeout) -> (status, headers,
text) | None (дефолт — requests: timeout=15, браузерный User-Agent, редиректы
follow'атся дефолтом requests) и вытаскивает текст readability-lxml. Дефолтный
fetch держит SSRF-гвард (URL приходит из чата: localhost/.local/.internal и
приватные IP до GET и после редиректов отклоняются RuntimeError'ом; лимит тела
10 МБ по Content-Length и по факту чтения) — RuntimeError'а гварда наружу не
глотаются (это отказ разбора, error-путь пайплайна). Не-2xx и молчание сети —
RuntimeError «страница недоступна» (сетевой сбой, error-путь пайплайна);
не-html контент и текст короче 500 знаков — NotAnArticle: страницы-меню и
кнопки интерфейса не статьи, вежливый отказ лучше «разбора» навигации, а после
ретрая статья там не появится. text/plain — исключение: body и есть текст,
readability не нужен, заголовок берётся из хвоста URL.
"""

import html
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import requests
from readability import Document

from bot.sources.base import NotAnArticle, SourceDocument

log = logging.getLogger("yt-summary")

_TIMEOUT = 15  # секунды на закачку страницы статьи; в env не выносится

# Порог «текста достаточно»: осмысленная заметка длиннее, меню/навигация — короче
_MIN_ARTICLE_CHARS = 500

# Лимит тела страницы: статья больше 10 МБ не бывает, а без лимита ссылка на
# дамп (бэкап, видео, ISO) выкачивается целиком и съедает память/трафик
_MAX_HTML_BYTES = 10 * 1024 * 1024

# UA обычного браузера: часть сайтов режет запросы без него (403/заглушка cookies)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Хвостовая пунктуация предложения, срезаемая с конца найденного URL (скобки
# «)»/«]»/«}» разбираются отдельно — по балансу с открывающими внутри URL)
_PUNCT_TAIL = set(">.,;:!?«»…\"'")

_CLOSERS = {")": "(", "]": "[", "}": "{"}

# Хостнеймы, которые всегда внутренние: URL приходит из чата, и ссылка вида
# http://169.254.169.254/... не должна становиться дверью во внутреннюю сеть
_INTERNAL_HOSTNAMES = {"localhost"}
_INTERNAL_SUFFIXES = (".local", ".internal")

# Ключи-трекеры с фиксированными именами; «utm_»-префикс покрывает
# utm_source/utm_medium/utm_campaign/utm_term/utm_content одним правилом
_TRACKING_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}

# Разбор <meta> — регулярками (как в bot/analysis/author.py): нужен только
# содержимое и имя свойства, полный XML-парсер страницы избыточен
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_CONTENT_RE = re.compile(r"""content\s*=\s*["'](.*?)["']""", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(r"""property\s*=\s*["']og:title["']""", re.IGNORECASE)
_OG_ARTICLE_AUTHOR_RE = re.compile(r"""property\s*=\s*["']og:article:author["']""", re.IGNORECASE)
_ARTICLE_AUTHOR_RE = re.compile(r"""property\s*=\s*["']article:author["']""", re.IGNORECASE)
_NAME_AUTHOR_RE = re.compile(r"""name\s*=\s*["']author["']""", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_LANG_RE = re.compile(r"""\blang\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _resolve_host_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Все IP hostname'а; сбой DNS — RuntimeError (гвард не может пропустить адрес,
    который не смог проверить)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as e:
        raise RuntimeError(f"не удалось проверить адрес {hostname}: {e}") from e
    ips = []
    for *_, sockaddr in infos:
        try:
            ips.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:  # непарсируемый ответ резолвера — не пропускаем тихо, проверим остальные
            continue
    return ips


def _assert_public_url(url: str) -> None:
    """SSRF-гвард дефолтного fetch: внутренние адреса не разбираются.

    URL приходит из чата, а не от оператора: без проверки ссылка вида
    http://169.254.169.254/latest/meta-data/ или http://localhost:8080/ давала
    бы боту читать внутреннюю сеть и метаданные облака. Проверяется и имя
    (localhost, *.local, *.internal), и ВСЕ адреса, в которые имя разрешается
    (loopback/приватные/link-local/reserved/multicast/unspecified). Вызывается
    до GET и после него (follow'нутый редирект может увести на приватный хост).
    DNS-rebinding (смена IP между проверкой и соединением) осознанно принят:
    окно мало, а честная защита требует пиннинга IP на уровне соединения, а не
    обёртки над requests.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise RuntimeError("внутренние адреса не разбираются")
    host = hostname.lower()
    if host in _INTERNAL_HOSTNAMES or host.endswith(_INTERNAL_SUFFIXES):
        raise RuntimeError("внутренние адреса не разбираются")
    for ip in _resolve_host_ips(host):
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise RuntimeError("внутренние адреса не разбираются")


def _read_limited(resp) -> str:
    """Тело ответа с лимитом _MAX_HTML_BYTES; превышение — RuntimeError.

    Content-Length проверяется ДО чтения (большой файл не качается вовсе), но
    заголовок бывает лживым или отсутствовать (chunked, сжатие) — лимит
    дублируется по факту чтения. requests при stream не всегда определяет
    кодировку (в Content-Type нет charset) — тогда utf-8 с заменой битых байт.
    """
    length = str(resp.headers.get("content-length") or "")
    if length.isdigit() and int(length) > _MAX_HTML_BYTES:
        raise RuntimeError("страница слишком большая")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > _MAX_HTML_BYTES:
            raise RuntimeError("страница слишком большая")
        chunks.append(chunk)
    body = b"".join(chunks)
    return body.decode(resp.encoding or "utf-8", errors="replace")


def fetch(url: str, timeout: float = _TIMEOUT) -> tuple[int, Any, str] | None:
    """Дефолтный fetch: (status, headers, text) или None при сетевой ошибке.

    Инжектируется в ArticleExtractor (в тестах — фейк), поэтому держит
    минимальный контракт; SSRF-гвард и лимит размера бросают RuntimeError
    НАРУЖУ (это отказ разбора — error-путь пайплайна), глотается только
    requests.RequestException («сеть легла» -> None). Редиректы follow'атся
    дефолтом requests, финальный адрес перепроверяется гвардом.
    """
    try:
        _assert_public_url(url)
        with requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS, stream=True) as resp:
            _assert_public_url(resp.url)
            text = _read_limited(resp)
            status, headers = resp.status_code, resp.headers
    except requests.RequestException as e:
        log.warning("fetch %s упал: %s", url, e)
        return None
    return status, headers, text


# Алиас нужен, потому что параметр __init__ экстрактора называется fetch и
# затеняет функцию внутри своего scope (см. bot/analysis/author.py)
_DEFAULT_FETCH = fetch


def _normalize_url(url: str) -> str:
    """Каноническая форма URL статьи (правила — в докстринге модуля)."""
    parsed = urlparse(url)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    # userinfo срезается: user:secret@example.com в ключе кеша не нужен, а
    # разные логины одной страницы — один и тот же источник
    host = parsed.netloc.rpartition("@")[2].lower()
    # params (;jsessionid=...) — часть пути, терять их нельзя (иначе canonical
    # страниц с сессионной меткой совпадёт и разборы склеятся)
    return urlunparse(("https", host, path, parsed.params, urlencode(query), ""))


def _trim_url_tail(text: str, start: int, end: int) -> int:
    """Срезает хвостовую пунктуацию текста с конца найденного URL (границы find).

    «<url>», «url.», «url)», «url»» — знаки предложения вокруг ссылки, а не
    часть адреса. Закрывающие скобки срезаются только если у них нет парной
    открывающей внутри URL: сбалансированное «wiki/X_(язык)» остаётся целиком.
    Баланс скобок считается заранее по всему URL — обрезка с конца его не
    меняет; «+1» в условии — сама закрывающая (баланс строго ЛЕВЕЕ неё).
    """
    balance = {"(": 0, "[": 0, "{": 0}
    for ch in text[start:end]:
        if ch in balance:
            balance[ch] += 1
        elif ch in _CLOSERS:
            balance[_CLOSERS[ch]] -= 1

    trimmed = dict.fromkeys(balance, 0)  # уже срезанные закрывающие по типам
    while end > start:
        ch = text[end - 1]
        if ch in _CLOSERS:
            opener = _CLOSERS[ch]
            if balance[opener] + trimmed[opener] + 1 > 0:
                break  # есть незакрытая открывающая внутри URL — скобка часть адреса
            trimmed[opener] += 1
            end -= 1
        elif ch in _PUNCT_TAIL:
            end -= 1
        else:
            break
    return end


class _SummaryText(HTMLParser):
    """Текст из readability-summary: блоки -> \\n\\n, br -> \\n, прочие теги срезаются.

    Атрибуты парсером в текст не попадают — от <a href="..."> остаётся только
    текст ссылки. Сущности раскрываются html.unescape'ом; script/style в текст
    не идут. 3+ перевода строки схлопываются до двух: между соседними блоками
    скапливаются и разделитель блока, и переводы строк исходной разметки.
    """

    _BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._skip_depth = 0

    @classmethod
    def extract_from(cls, html_text: str) -> str:
        parser = cls()
        parser.feed(html_text)
        parser.close()
        return parser.text()

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag in self._SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(html.unescape(data))

    def handle_entityref(self, name):
        if not self._skip_depth:
            self._parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        if not self._skip_depth:
            self._parts.append(html.unescape(f"&#{name};"))

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._parts)).strip()


def _meta_content(page: str, matcher: re.Pattern) -> str:
    """content первого <meta>, чей тег матчится matcher'ом; порядок атрибутов не важен."""
    for tag in _META_TAG_RE.findall(page):
        if not matcher.search(tag):
            continue
        match = _CONTENT_RE.search(tag)
        if match:
            value = html.unescape(match.group(1)).strip()
            if value:
                return value
    return ""


def _article_author(page: str) -> str:
    """Автор статьи: цепочка og:article:author | article:author | <meta name="author">."""
    for matcher in (_OG_ARTICLE_AUTHOR_RE, _ARTICLE_AUTHOR_RE, _NAME_AUTHOR_RE):
        found = _meta_content(page, matcher)
        if found:
            return found
    return ""


def _html_lang(page: str) -> str:
    """Значение lang у тега <html>; тега или атрибута нет — пустая строка."""
    html_tag = _HTML_TAG_RE.search(page)
    if not html_tag:
        return ""
    match = _LANG_RE.search(html_tag.group(0))
    return match.group(1).strip() if match else ""


def _title_from_url(url: str) -> str:
    """Заголовок для text/plain: последний сегмент пути (мета-тегов у файла нет)."""
    parsed = urlparse(url)
    tail = unquote(parsed.path).rstrip("/").rsplit("/", 1)[-1]
    return tail or parsed.netloc


class ArticleExtractor:
    """Fallback-экстрактор статей за портом ExtractorPort (ARCHITECTURE.md §5-§6).

    find — основной метод порта (границы совпадения для Router), matches
    производен от него; canonical_id — «article:<norm-url>», ключ кеша отчётов.
    Сеть трогает только extract через fetch (инжектируется в тестах).
    """

    name = "article"

    def __init__(self, fetch=None):
        # подмена на уровне инстанса, глобальное состояние не трогаем
        self._fetch = fetch or _DEFAULT_FETCH

    def find(self, text: str) -> tuple[int, int] | None:
        """Границы первой http(s)-ссылки в тексте или None (fallback: подходит любая).

        Хвостовая пунктуация предложения («<url>», «url.», «url)») срезается —
        иначе Router передаст в разбор знак препинания как часть адреса.
        """
        m = _URL_RE.search(text or "")
        if not m:
            return None
        return (m.start(), _trim_url_tail(text, m.start(), m.end()))

    def matches(self, url: str) -> bool:
        """Производный предикат порта: find(url) нашёл ссылку."""
        return self.find(url) is not None

    def canonical_id(self, url: str) -> str | None:
        """«article:<norm-url>» — ключ кеша отчётов (нормализация в докстринге модуля)."""
        return f"article:{_normalize_url(url)}"

    def extract(self, url: str) -> SourceDocument:
        """Страница -> SourceDocument(kind="article"); сбой добычи — исключение наружу."""
        fetched = self._fetch(url, _TIMEOUT)
        if fetched is None:
            # сеть легла целиком — сетевой сбой (error-путь), а не «статьи нет»
            raise RuntimeError("страница недоступна: сайт не ответил")
        status, headers, page = fetched
        if not 200 <= status < 300:
            raise RuntimeError(f"страница недоступна: HTTP {status}")
        content_type = str(headers.get("content-type", "") or "").lower()
        if not content_type.startswith(("text/html", "application/xhtml+xml", "text/plain")):
            # pdf/картинка/JSON: статьи здесь нет и после ретрая не будет
            raise NotAnArticle(
                f"по ссылке не html-страница (content-type: {content_type or 'не задан'})"
            )

        if content_type.startswith("text/plain"):
            # весь body и есть текст: readability и срезание тегов не нужны,
            # заголовка и мета у файла нет — title из хвоста URL, lang пустой
            if len(page) < _MIN_ARTICLE_CHARS:
                raise NotAnArticle(f"текст страницы слишком короткий ({len(page)} знаков)")
            return SourceDocument(
                canonical_id=self.canonical_id(url),
                kind="article",
                url=url,
                title=_title_from_url(url),
                content=page,
                lang="",
                data={"author": "", "site": urlparse(url).netloc},
            )

        doc = Document(page)
        # заголовок: короткий <title> readability (хвост сайта отрезан);
        # тега <title> нет — берём og:title
        title = doc.short_title() or _meta_content(page, _OG_TITLE_RE)
        text = _SummaryText.extract_from(doc.summary(html_partial=True))
        if len(text) < _MIN_ARTICLE_CHARS:
            # страницы-меню/навигация не статьи: вежливый отказ вместо «разбора»
            raise NotAnArticle(f"текст страницы слишком короткий ({len(text)} знаков)")

        return SourceDocument(
            canonical_id=self.canonical_id(url),
            kind="article",
            url=url,
            title=title,
            content=text,
            lang=_html_lang(page),
            data={"author": _article_author(page), "site": urlparse(url).netloc},
        )
