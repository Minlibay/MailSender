"""Персонализация письма: подстановка полей и обязательный подвал.

Плейсхолдеры в теме и теле: {{first_name}}, {{last_name}}, {{company}},
{{email}} и любые произвольные поля контакта из fields_json.

Пустые/отсутствующие поля можно заменить дефолтом через синтаксис
{{first_name|Коллеги}} — если имени нет, подставится «Коллеги».
"""

from __future__ import annotations

import html
import json
import re

_PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*(?:\|([^}]*))?\}\}")


def contact_context(contact) -> dict[str, str]:
    """Собрать словарь подстановки из строки контакта (sqlite3.Row или dict)."""
    get = contact.__getitem__ if hasattr(contact, "keys") else contact.get
    ctx = {
        "email": _s(get("email")),
        "first_name": _s(get("first_name")),
        "last_name": _s(get("last_name")),
        "company": _s(get("company")),
    }
    raw = _s(get("fields_json")) or "{}"
    try:
        extra = json.loads(raw)
        if isinstance(extra, dict):
            for k, v in extra.items():
                ctx.setdefault(str(k), "" if v is None else str(v))
    except (json.JSONDecodeError, TypeError):
        pass
    return ctx


def _s(v) -> str:
    return "" if v is None else str(v)


def render(template: str, ctx: dict[str, str]) -> str:
    """Подставить {{...}} в шаблон. Неизвестные плейсхолдеры → пусто/дефолт."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        default = m.group(2)
        value = ctx.get(key, "")
        if not value and default is not None:
            return default
        return value
    return _PLACEHOLDER.sub(repl, template or "")


def find_placeholders(template: str) -> set[str]:
    """Все имена плейсхолдеров, встречающиеся в шаблоне — для валидации в UI."""
    return {m.group(1) for m in _PLACEHOLDER.finditer(template or "")}


# ---------------- подпись отправителя (необязательная) ----------------
# Модель — 1-to-1 B2B-аутрич: одно персональное письмо с рабочего ящика,
# дальше менеджер ведёт переписку сам. Поэтому никакого блока «отписаться»
# в письме нет — обычное деловое письмо его не содержит. Подпись с названием
# компании/адресом добавляется, только если эти поля заполнены.

def build_signature_text(sender) -> str:
    # Приоритет — личная подпись менеджера (свободный текст).
    sig = (getattr(sender, "signature", "") or "").strip()
    if sig:
        return "\n\n—\n" + sig
    lines = []
    if sender.org_name:
        lines.append(sender.org_name)
    if sender.postal_address:
        lines.append(sender.postal_address)
    if not lines:
        return ""
    return "\n\n—\n" + "\n".join(lines)


def build_signature_html(sender) -> str:
    sig = (getattr(sender, "signature", "") or "").strip()
    if sig:
        body = html.escape(sig).replace("\n", "<br>")
    else:
        lines = []
        if sender.org_name:
            lines.append(html.escape(sender.org_name))
        if sender.postal_address:
            lines.append(html.escape(sender.postal_address))
        if not lines:
            return ""
        body = "<br>".join(lines)
    return ('<hr style="border:none;border-top:1px solid #ccc;margin:24px 0 8px">'
            f'<div style="font-size:12px;color:#888;line-height:1.5">{body}</div>')


def render_message(campaign, contact, sender) -> tuple[str, str, str]:
    """Собрать финальные (subject, text, html) для конкретного контакта.

    Подпись добавляется, только если заполнены реквизиты отправителя.
    Если html-части нет, возвращается пустая строка.
    """
    ctx = contact_context(contact)
    subject = render(_field(campaign, "subject"), ctx)
    text_body = render(_field(campaign, "body_text"), ctx)
    html_body = render(_field(campaign, "body_html"), ctx)

    text_out = text_body + build_signature_text(sender) if text_body else ""
    html_out = html_body + build_signature_html(sender) if html_body else ""
    return subject, text_out, html_out


def _field(obj, name):
    try:
        return obj[name]
    except (KeyError, IndexError, TypeError):
        return getattr(obj, name, "")
