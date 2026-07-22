"""Поиск контактного адреса ОДНОЙ компании по её сайту.

Назначение — помочь менеджеру найти публичный адрес (info@, hello@, pr@)
на сайте конкретной компании для персонального письма. Это не харвестер:

  * за один вызов обрабатывается один домен (URL, который ввёл менеджер);
  * проверяется заданная страница + небольшой набор типовых страниц контактов
    на том же домене (не спайдерим весь сайт и не ходим по внешним ссылкам);
  * число страниц ограничено, результат отдаётся человеку на выбор — ничего
    не добавляется в рассылку автоматически.

Пакетный обход списков доменов сознательно не реализован.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from .contacts import normalize_email

# Типовые страницы, где компании публикуют контактный адрес (тот же домен).
_CONTACT_PATHS = [
    "/contact", "/contacts", "/contact-us", "/contactus",
    "/about", "/about-us", "/company",
    "/kontakty", "/kontakti", "/kontakt", "/o-nas", "/contacts.html",
]

# Роли контактных ящиков — показываем их выше остальных.
_ROLE_ORDER = ["info", "hello", "hi", "pr", "press", "sales", "contact",
               "office", "mail", "support", "team", "welcome", "ask"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAILTO_RE = re.compile(r"mailto:([^\"'>\s?]+)", re.I)

# Явный мусор, который часто попадает в HTML/скрипты.
_BAD_SUBSTR = ("example.", "@example", "sentry", "wixpress", "@sentry",
               "your-email", "youremail", "email@", "@domain", "domain.com",
               "@2x", "@3x", "core-js", ".png", ".jpg", ".jpeg", ".gif",
               ".svg", ".webp", ".css", ".js", "u00", "test@test")

_MAX_PAGES = 6
_TIMEOUT = 10
_MAX_BYTES = 1_500_000
_UA = "Mozilla/5.0 (compatible; MailSender-ContactFinder/1.0)"


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _registrable(host: str) -> str:
    """Грубый «домен» для сравнения: без www."""
    return host.lower().removeprefix("www.")


def _fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            ctype = resp.headers.get_content_type()
            if ctype and not ctype.startswith(("text/", "application/xhtml")):
                return None
            data = resp.read(_MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _extract(html: str) -> set[str]:
    emails: set[str] = set()
    for m in _MAILTO_RE.findall(html):
        emails.add(m.split("?")[0].strip().lower())
    for m in _EMAIL_RE.findall(html):
        emails.add(m.strip().lower())
    return emails


def _looks_valid(email: str) -> bool:
    if len(email) > 100 or any(b in email for b in _BAD_SUBSTR):
        return False
    return normalize_email(email) is not None


def _role_rank(email: str) -> int:
    local = email.split("@", 1)[0]
    try:
        return _ROLE_ORDER.index(local)
    except ValueError:
        return len(_ROLE_ORDER)


def find_site_emails(url: str, *, max_pages: int = _MAX_PAGES) -> dict:
    """Найти публичные адреса на сайте одной компании.

    Возвращает:
      { ok, url, domain, pages_checked: [...],
        primary: [адреса на домене сайта], other: [прочие найденные] }
    primary отсортированы по «ролям» (info/hello/pr… — первыми).
    """
    if not url or not url.strip():
        return {"ok": False, "message": "Укажите адрес сайта"}

    start = _normalize_url(url)
    parsed = urlparse(start)
    if not parsed.netloc:
        return {"ok": False, "message": "Некорректный адрес сайта"}

    base = f"{parsed.scheme}://{parsed.netloc}"
    site_domain = _registrable(parsed.netloc)

    # заданная страница + типовые контактные страницы того же домена
    candidates = [start] + [urljoin(base, p) for p in _CONTACT_PATHS]

    found: set[str] = set()
    checked: list[str] = []
    seen: set[str] = set()
    for u in candidates:
        if len(checked) >= max_pages:
            break
        if u in seen:
            continue
        seen.add(u)
        html = _fetch(u)
        if html is None:
            continue
        checked.append(u)
        found.update(_extract(html))

    valid = sorted({e for e in found if _looks_valid(e)})
    primary = sorted((e for e in valid if _registrable(e.split("@")[1]) == site_domain),
                     key=lambda e: (_role_rank(e), e))
    other = [e for e in valid if _registrable(e.split("@")[1]) != site_domain]

    if not checked:
        return {"ok": False, "message": "Не удалось открыть сайт "
                "(проверьте адрес или доступность)."}
    return {
        "ok": True,
        "url": start,
        "domain": site_domain,
        "pages_checked": checked,
        "primary": primary,
        "other": other,
    }
