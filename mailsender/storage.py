"""Хранилище на SQLite: контакты, кампании, лог доставки, стоп-лист.

Одна БД в профиле пользователя. Все операции — через тонкий слой
функций, чтобы UI не знал про SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    first_name  TEXT DEFAULT '',
    last_name   TEXT DEFAULT '',
    company     TEXT DEFAULT '',
    fields_json TEXT DEFAULT '{}',   -- произвольные поля для подстановки
    status      TEXT DEFAULT 'active', -- active | bounced | unsubscribed
    source      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    email       TEXT PRIMARY KEY,
    reason      TEXT DEFAULT '',       -- unsubscribed | bounced | complaint | manual
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body_text   TEXT DEFAULT '',
    body_html   TEXT DEFAULT '',
    status      TEXT DEFAULT 'draft',  -- draft | sending | done | stopped
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER,
    contact_id  INTEGER,
    email       TEXT NOT NULL,
    status      TEXT NOT NULL,         -- sent | failed | skipped
    error       TEXT DEFAULT '',
    sent_at     TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    subject     TEXT DEFAULT '',
    body_text   TEXT DEFAULT '',
    body_html   TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- sent | replied | bounced | note | status | import | found
    detail      TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_send_log_campaign ON send_log(campaign_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_activity_contact ON activity(contact_id);
"""

# Колонки, добавляемые к contacts по мере развития (миграция ALTER TABLE).
_CONTACT_COLUMNS = {
    "notes": "TEXT DEFAULT ''",
    "last_sent_at": "TEXT",
    "last_reply_at": "TEXT",
    "last_reply_snippet": "TEXT DEFAULT ''",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Обёртка над одним соединением SQLite (потокобезопасная через lock)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Добавить недостающие колонки в contacts (для уже существующих БД)."""
        cur = self._conn.execute("PRAGMA table_info(contacts)")
        existing = {r["name"] for r in cur.fetchall()}
        for col, decl in _CONTACT_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # ---------------- контакты ----------------

    def upsert_contact(self, email: str, *, first_name="", last_name="",
                       company="", fields: dict | None = None, source="") -> tuple[int, bool]:
        """Добавить/обновить контакт по email. Возвращает (id, is_new)."""
        email = email.strip().lower()
        fields_json = json.dumps(fields or {}, ensure_ascii=False)
        with self._cursor() as cur:
            cur.execute("SELECT id FROM contacts WHERE email = ?", (email,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE contacts SET first_name=?, last_name=?, company=?,
                       fields_json=?, source=? WHERE id=?""",
                    (first_name, last_name, company, fields_json, source, row["id"]),
                )
                return row["id"], False
            cur.execute(
                """INSERT INTO contacts (email, first_name, last_name, company,
                   fields_json, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (email, first_name, last_name, company, fields_json, source, _now()),
            )
            return cur.lastrowid, True

    def list_contacts(self, *, status: str | None = None, limit=None, offset=0) -> list[sqlite3.Row]:
        q = "SELECT * FROM contacts"
        params: list = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY id"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        with self._cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def count_contacts(self, *, status: str | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM contacts"
        params: list = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        with self._cursor() as cur:
            cur.execute(q, params)
            return cur.fetchone()["n"]

    def set_contact_status(self, email: str, status: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE contacts SET status=? WHERE email=?",
                        (status, email.strip().lower()))

    def delete_contact(self, contact_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE id=?", (contact_id,))

    def get_contact(self, contact_id: int) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id=?", (contact_id,))
            return cur.fetchone()

    def get_contact_by_email(self, email: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE email=?", (email.strip().lower(),))
            return cur.fetchone()

    def set_notes(self, contact_id: int, notes: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE contacts SET notes=? WHERE id=?", (notes, contact_id))

    # ---------------- статусы отправки/ответа/отскока ----------------

    def mark_sent(self, email: str) -> None:
        """Успешно отправлено: контакт переходит в 'sent', ставится дата."""
        email = email.strip().lower()
        with self._cursor() as cur:
            cur.execute(
                """UPDATE contacts SET status='sent', last_sent_at=?
                   WHERE email=? AND status='active'""",
                (_now(), email),
            )
            cur.execute(
                """UPDATE contacts SET last_sent_at=? WHERE email=?""",
                (_now(), email),
            )
            self._log_activity(cur, email, "sent", "письмо отправлено")

    def mark_replied(self, email: str, snippet: str = "") -> bool:
        """Получен ответ: контакт → 'replied'. Возвращает True, если контакт найден."""
        email = email.strip().lower()
        with self._cursor() as cur:
            cur.execute("SELECT id FROM contacts WHERE email=?", (email,))
            if cur.fetchone() is None:
                return False
            cur.execute(
                """UPDATE contacts SET status='replied', last_reply_at=?,
                   last_reply_snippet=? WHERE email=?""",
                (_now(), snippet[:300], email),
            )
            self._log_activity(cur, email, "replied", snippet[:200])
            return True

    def mark_bounced(self, email: str) -> None:
        email = email.strip().lower()
        with self._cursor() as cur:
            cur.execute("UPDATE contacts SET status='bounced' WHERE email=?", (email,))
            self._log_activity(cur, email, "bounced", "письмо не доставлено")

    def contacts_needing_followup(self, days: int) -> list[sqlite3.Row]:
        """Отправленные >= days назад и без ответа — кандидаты на повторное касание."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM contacts
                   WHERE status='sent' AND last_sent_at IS NOT NULL
                   AND last_sent_at <= ? ORDER BY last_sent_at""",
                (cutoff,),
            )
            return cur.fetchall()

    # ---------------- активность (таймлайн) ----------------

    def _log_activity(self, cur, email: str, kind: str, detail: str = "") -> None:
        cur.execute("SELECT id FROM contacts WHERE email=?", (email.strip().lower(),))
        row = cur.fetchone()
        if row:
            cur.execute(
                "INSERT INTO activity (contact_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], kind, detail, _now()),
            )

    def add_activity(self, contact_id: int, kind: str, detail: str = "") -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO activity (contact_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (contact_id, kind, detail, _now()),
            )

    def list_activity(self, contact_id: int, limit=50) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM activity WHERE contact_id=? ORDER BY id DESC LIMIT ?",
                (contact_id, limit),
            )
            return cur.fetchall()

    # ---------------- шаблоны писем ----------------

    def save_template(self, name, subject, body_text="", body_html="", template_id=None) -> int:
        with self._cursor() as cur:
            if template_id:
                cur.execute(
                    """UPDATE templates SET name=?, subject=?, body_text=?, body_html=?,
                       updated_at=? WHERE id=?""",
                    (name, subject, body_text, body_html, _now(), template_id),
                )
                return template_id
            cur.execute(
                """INSERT INTO templates (name, subject, body_text, body_html, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, subject, body_text, body_html, _now(), _now()),
            )
            return cur.lastrowid

    def list_templates(self) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM templates ORDER BY updated_at DESC")
            return cur.fetchall()

    def get_template(self, template_id) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM templates WHERE id=?", (template_id,))
            return cur.fetchone()

    def delete_template(self, template_id) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM templates WHERE id=?", (template_id,))

    # ---------------- стоп-лист ----------------

    def add_suppression(self, email: str, reason="manual") -> None:
        email = email.strip().lower()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO suppression (email, reason, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET reason=excluded.reason""",
                (email, reason, _now()),
            )
            # контакт помечаем, чтобы не попал в выборку рассылки; статус
            # отражает причину (отскок — это не отписка).
            new_status = "bounced" if reason == "bounced" else "unsubscribed"
            cur.execute("UPDATE contacts SET status=? WHERE email=?", (new_status, email))

    def is_suppressed(self, email: str) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM suppression WHERE email=?", (email.strip().lower(),))
            return cur.fetchone() is not None

    def suppressed_set(self) -> set[str]:
        with self._cursor() as cur:
            cur.execute("SELECT email FROM suppression")
            return {r["email"] for r in cur.fetchall()}

    def list_suppression(self) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM suppression ORDER BY created_at DESC")
            return cur.fetchall()

    def remove_suppression(self, email: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM suppression WHERE email=?", (email.strip().lower(),))

    # ---------------- кампании ----------------

    def create_campaign(self, name, subject, body_text="", body_html="") -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO campaigns (name, subject, body_text, body_html, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, subject, body_text, body_html, _now()),
            )
            return cur.lastrowid

    def update_campaign(self, campaign_id, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._cursor() as cur:
            cur.execute(f"UPDATE campaigns SET {cols} WHERE id=?",
                        [*fields.values(), campaign_id])

    def get_campaign(self, campaign_id) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
            return cur.fetchone()

    def list_campaigns(self) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
            return cur.fetchall()

    # ---------------- лог доставки ----------------

    def log_send(self, campaign_id, contact_id, email, status, error="") -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO send_log (campaign_id, contact_id, email, status, error, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (campaign_id, contact_id, email, status, error, _now()),
            )

    def already_sent_emails(self, campaign_id) -> set[str]:
        """Адреса, которым в этой кампании уже успешно отправлено — для докидывания."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT email FROM send_log WHERE campaign_id=? AND status='sent'",
                (campaign_id,),
            )
            return {r["email"] for r in cur.fetchall()}

    def sent_count_since(self, iso_time: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM send_log WHERE status='sent' AND sent_at >= ?",
                (iso_time,),
            )
            return cur.fetchone()["n"]

    def campaign_stats(self, campaign_id) -> dict:
        with self._cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) AS n FROM send_log
                   WHERE campaign_id=? GROUP BY status""",
                (campaign_id,),
            )
            stats = {"sent": 0, "failed": 0, "skipped": 0}
            for r in cur.fetchall():
                stats[r["status"]] = r["n"]
            return stats

    def campaign_log(self, campaign_id, limit=500) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM send_log WHERE campaign_id=? ORDER BY id DESC LIMIT ?",
                (campaign_id, limit),
            )
            return cur.fetchall()
