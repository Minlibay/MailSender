"""Конфигурация приложения: настройки SMTP/IMAP, отправителя и лимиты.

Пароль в JSON не хранится — он кладётся в системное хранилище (keyring).
Если keyring недоступен, приложение всё равно работает: пароль просто
не запоминается между запусками и запрашивается заново.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import keyring
except Exception:  # keyring не установлен/не настроен — не критично
    keyring = None

APP_NAME = "MailSender"
_KEYRING_SERVICE = "MailSender-SMTP"


def config_dir() -> Path:
    """Каталог настроек/данных.

    В Docker путь задаётся через MAILSENDER_DATA_DIR (монтируется как том),
    иначе — профиль пользователя (кроссплатформенно).
    """
    override = os.environ.get("MAILSENDER_DATA_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "settings.json"


def db_path() -> Path:
    return config_dir() / "mailsender.db"


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    use_tls: bool = True          # STARTTLS на 587
    use_ssl: bool = False         # SSL/TLS на 465


@dataclass
class ImapConfig:
    host: str = ""
    port: int = 993
    username: str = ""
    use_ssl: bool = True


@dataclass
class SenderConfig:
    from_name: str = ""
    from_email: str = ""
    reply_to: str = ""
    # Подпись менеджера — добавляется в конец письма (имя, должность, телефон
    # и т.п. свободным текстом). Если пусто, используется org/адрес как раньше.
    signature: str = ""
    org_name: str = ""
    postal_address: str = ""
    unsubscribe_mailto: str = ""   # не используется (оставлено для совместимости)


@dataclass
class SendingLimits:
    per_hour: int = 100            # мягкий лимит, чтобы не ловить блокировки
    per_day: int = 500
    delay_seconds: float = 3.0     # пауза между письмами
    batch_size: int = 50           # размер пачки перед длинной паузой
    batch_pause_seconds: float = 60.0


@dataclass
class AppConfig:
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    imap: ImapConfig = field(default_factory=ImapConfig)
    sender: SenderConfig = field(default_factory=SenderConfig)
    limits: SendingLimits = field(default_factory=SendingLimits)

    # ---- сериализация ----
    def to_dict(self) -> dict:
        return {
            "smtp": asdict(self.smtp),
            "imap": asdict(self.imap),
            "sender": asdict(self.sender),
            "limits": asdict(self.limits),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        cfg = cls()
        if "smtp" in data:
            cfg.smtp = SmtpConfig(**{**asdict(cfg.smtp), **data["smtp"]})
        if "imap" in data:
            cfg.imap = ImapConfig(**{**asdict(cfg.imap), **data["imap"]})
        if "sender" in data:
            cfg.sender = SenderConfig(**{**asdict(cfg.sender), **data["sender"]})
        if "limits" in data:
            cfg.limits = SendingLimits(**{**asdict(cfg.limits), **data["limits"]})
        return cfg

    # ---- файл ----
    def save(self) -> None:
        config_path().write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls()


# ---- пароль SMTP ----
# Приоритет источников: env-переменная > системное хранилище (desktop) >
# файл в каталоге данных (Docker, где keyring недоступен).

_ENV_SMTP_PASSWORD = "MAILSENDER_SMTP_PASSWORD"


def _password_file() -> Path:
    return config_dir() / "smtp_password"


def save_smtp_password(username: str, password: str) -> bool:
    """Сохранить пароль. Возвращает True при успехе (хоть одним способом)."""
    if not username:
        return False
    if keyring:
        try:
            keyring.set_password(_KEYRING_SERVICE, username, password)
            return True
        except Exception:
            pass
    # fallback для контейнера: файл с ограниченными правами
    try:
        p = _password_file()
        p.write_text(password, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_smtp_password(username: str) -> str | None:
    env = os.environ.get(_ENV_SMTP_PASSWORD)
    if env:
        return env
    if keyring and username:
        try:
            pw = keyring.get_password(_KEYRING_SERVICE, username)
            if pw:
                return pw
        except Exception:
            pass
    try:
        p = _password_file()
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def delete_smtp_password(username: str) -> None:
    if keyring and username:
        try:
            keyring.delete_password(_KEYRING_SERVICE, username)
        except Exception:
            pass
    try:
        _password_file().unlink(missing_ok=True)
    except OSError:
        pass
