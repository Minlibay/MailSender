"""IMAP-клиент: чтение входящих ответов на аутрич.

Используется, чтобы видеть ответы адресатов прямо в приложении и
автоматически ловить письма с темой/текстом об отписке, добавляя их
в стоп-лист. POP3 намеренно не основной: IMAP не удаляет письма с
сервера и позволяет читать статусы, оставаясь неразрушающим.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

_UNSUB_RE = re.compile(r"\b(unsubscribe|отпис|не\s+писать|remove\s+me|stop)\b", re.I)
_BOUNCE_SUBJECT_RE = re.compile(
    r"(mail delivery|delivery status|undeliverable|delivery failure|"
    r"returned mail|failure notice|не доставлено|недоставлен|"
    r"message not delivered)", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _is_bounce(reply) -> bool:
    frm = (reply.from_email or "").lower()
    return ("mailer-daemon" in frm or "postmaster" in frm
            or bool(_BOUNCE_SUBJECT_RE.search(reply.subject or "")))


class ImapError(Exception):
    pass


@dataclass
class Reply:
    uid: str
    from_email: str
    from_name: str
    subject: str
    date: datetime | None
    snippet: str
    is_unsubscribe: bool


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class ImapReader:
    def __init__(self, imap_cfg, password: str):
        self._cfg = imap_cfg
        self._password = password
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        cfg = self._cfg
        if not cfg.host:
            raise ImapError("Не указан IMAP-хост")
        try:
            if cfg.use_ssl:
                self._conn = imaplib.IMAP4_SSL(cfg.host, cfg.port)
            else:
                self._conn = imaplib.IMAP4(cfg.host, cfg.port)
            self._conn.login(cfg.username, self._password)
        except (imaplib.IMAP4.error, OSError) as e:
            raise ImapError(f"Не удалось подключиться к IMAP: {e}") from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def fetch_recent(self, *, folder="INBOX", limit=50, unseen_only=False) -> list[Reply]:
        """Прочитать последние письма. Не помечает их прочитанными (BODY.PEEK)."""
        if self._conn is None:
            raise ImapError("Нет соединения IMAP")
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"Не удалось открыть папку {folder}")

        criterion = "UNSEEN" if unseen_only else "ALL"
        typ, data = self._conn.search(None, criterion)
        if typ != "OK":
            raise ImapError("Ошибка поиска писем")
        uids = data[0].split()
        uids = uids[-limit:] if limit else uids

        replies: list[Reply] = []
        for uid in reversed(uids):
            typ, msg_data = self._conn.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            name, addr = parseaddr(msg.get("From", ""))
            subject = _decode(msg.get("Subject"))
            try:
                date = parsedate_to_datetime(msg.get("Date"))
            except (TypeError, ValueError):
                date = None
            snippet = self._extract_snippet(msg)
            is_unsub = bool(_UNSUB_RE.search(subject) or _UNSUB_RE.search(snippet))
            replies.append(Reply(
                uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                from_email=addr.lower(),
                from_name=_decode(name),
                subject=subject,
                date=date,
                snippet=snippet[:400],
                is_unsubscribe=is_unsub,
            ))
        return replies

    @staticmethod
    def _extract_snippet(msg) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace").strip()
                    except Exception:
                        continue
            return ""
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()
        except Exception:
            pass
        return ""


def sync_unsubscribes(storage, imap_cfg, password: str, *, limit=100) -> list[str]:
    """Прочитать входящие, найти письма-отписки и внести адреса в стоп-лист.

    Возвращает список адресов, добавленных в стоп-лист.
    """
    added: list[str] = []
    with ImapReader(imap_cfg, password) as reader:
        for reply in reader.fetch_recent(limit=limit):
            if reply.is_unsubscribe and reply.from_email:
                if not storage.is_suppressed(reply.from_email):
                    storage.add_suppression(reply.from_email, reason="unsubscribed")
                    added.append(reply.from_email)
    return added


def sync_replies(storage, imap_cfg, password: str, *, limit=100) -> dict:
    """Прочитать входящие и связать их с контактами на доске.

    Для каждого письма:
      * если это отказ («отписаться/stop») — адрес в стоп-лист;
      * иначе, если адрес есть среди контактов — контакт → «Ответили»,
        на карточке сохраняется фрагмент ответа.
    Возвращает {'replied': [...], 'unsubscribed': [...]}.
    """
    replied: list[str] = []
    unsubscribed: list[str] = []
    bounced: list[str] = []
    with ImapReader(imap_cfg, password) as reader:
        for reply in reader.fetch_recent(limit=limit):
            if not reply.from_email:
                continue

            # Отскок (bounce): письмо от mailer-daemon/postmaster или с темой
            # о недоставке — вытаскиваем из отчёта адрес, который есть в базе.
            if _is_bounce(reply):
                for addr in {a.lower() for a in _EMAIL_RE.findall(reply.snippet)}:
                    if storage.get_contact_by_email(addr) is not None:
                        storage.mark_bounced(addr)
                        if not storage.is_suppressed(addr):
                            storage.add_suppression(addr, reason="bounced")
                        bounced.append(addr)
                continue

            if reply.is_unsubscribe:
                if not storage.is_suppressed(reply.from_email):
                    storage.add_suppression(reply.from_email, reason="unsubscribed")
                    unsubscribed.append(reply.from_email)
                continue

            contact = storage.get_contact_by_email(reply.from_email)
            if contact is not None and contact["status"] != "replied":
                if storage.mark_replied(reply.from_email, reply.snippet):
                    replied.append(reply.from_email)
    return {"replied": replied, "unsubscribed": unsubscribed, "bounced": bounced}


def test_connection(imap_cfg, password: str) -> tuple[bool, str]:
    reader = ImapReader(imap_cfg, password)
    try:
        reader.connect()
        reader.close()
        return True, "Соединение с IMAP успешно."
    except ImapError as e:
        return False, str(e)
