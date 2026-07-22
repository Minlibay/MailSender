"""SMTP-клиент: отправка через корпоративный сервер.

Держит одно соединение живым на всю рассылку (эффективнее, чем логиниться
на каждое письмо). Поддерживает STARTTLS (587) и SSL/TLS (465).
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


class SmtpError(Exception):
    """Ошибка отправки/соединения SMTP с человекочитаемым текстом."""


class SmtpSender:
    def __init__(self, smtp_cfg, sender_cfg, password: str):
        self._cfg = smtp_cfg
        self._sender = sender_cfg
        self._password = password
        self._conn: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    # ---- соединение ----

    def connect(self) -> None:
        cfg = self._cfg
        if not cfg.host:
            raise SmtpError("Не указан SMTP-хост")
        try:
            if cfg.use_ssl:
                ctx = ssl.create_default_context()
                self._conn = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=ctx)
            else:
                self._conn = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
                self._conn.ehlo()
                if cfg.use_tls:
                    ctx = ssl.create_default_context()
                    self._conn.starttls(context=ctx)
                    self._conn.ehlo()
            if cfg.username:
                self._conn.login(cfg.username, self._password)
        except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
            raise SmtpError(f"Не удалось подключиться к SMTP: {e}") from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- отправка ----

    def build_message(self, to_email, subject, text_body, html_body="") -> EmailMessage:
        # Письмо оформляется как обычное личное деловое (1-to-1 аутрич):
        # без List-Unsubscribe и прочих признаков массовой рассылки.
        msg = EmailMessage()
        from_email = self._sender.from_email or self._cfg.username
        msg["From"] = formataddr((self._sender.from_name or "", from_email))
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        if self._sender.reply_to:
            msg["Reply-To"] = self._sender.reply_to

        msg.set_content(text_body or "")
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        return msg

    def send(self, msg: EmailMessage) -> None:
        if self._conn is None:
            raise SmtpError("Нет соединения SMTP (вызовите connect())")
        try:
            self._conn.send_message(msg)
        except smtplib.SMTPServerDisconnected:
            # сервер разорвал keep-alive — переподключаемся один раз
            self.connect()
            self._conn.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise SmtpError(f"Адрес отклонён сервером: {e.recipients}") from e
        except smtplib.SMTPException as e:
            raise SmtpError(f"Ошибка отправки: {e}") from e

    def send_simple(self, to_email, subject, text_body, html_body="") -> None:
        msg = self.build_message(to_email, subject, text_body, html_body)
        self.send(msg)


def test_connection(smtp_cfg, sender_cfg, password: str) -> tuple[bool, str]:
    """Проверить настройки соединения. Возвращает (успех, сообщение)."""
    sender = SmtpSender(smtp_cfg, sender_cfg, password)
    try:
        sender.connect()
        sender.close()
        return True, "Соединение с SMTP успешно, авторизация прошла."
    except SmtpError as e:
        return False, str(e)
