"""Мост между веб-интерфейсом (JS) и Python-бэкендом.

Класс Api экспонируется в pywebview как `window.pywebview.api.*`.
Каждый метод возвращает JSON-сериализуемые данные (dict/list/примитивы).
Тяжёлый Python-код (SMTP/IMAP/БД) остаётся общим с остальным приложением —
веб-слой только рисует интерфейс.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from . import config as cfg_mod
from . import contacts as contacts_mod
from . import templates
from .campaign import CampaignRunner
from .imap_client import ImapError, ImapReader, sync_replies, sync_unsubscribes
from .imap_client import test_connection as imap_test
from .sequence_runner import SequenceScheduler
from .smtp_client import SmtpError, SmtpSender
from .smtp_client import test_connection as smtp_test
from .storage import Storage

# Соответствие статусов контактов колонкам канбан-доски.
BOARD_COLUMNS = [
    ("active", "Новые"),
    ("sent", "Отправлено"),
    ("replied", "Ответили"),
    ("unsubscribed", "Отписались"),
]

# Через сколько дней без ответа контакт из «Отправлено» помечается как
# требующий повторного касания (follow-up).
FOLLOWUP_DAYS = 4


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


class Api:
    def __init__(self):
        self.config = cfg_mod.AppConfig.load()
        self.storage = Storage()
        self.window = None                 # выставляется в desktop-режиме (pywebview)
        self.emit = None                   # hook событий (event, payload) для web/SSE
        self.runner: CampaignRunner | None = None
        self.campaign_id: int | None = None
        self._password_mem: str = ""       # пароль на время сессии, если нет keyring
        # Планировщик цепочек писем (запускается через start_scheduler()).
        self.scheduler = SequenceScheduler(
            self.storage,
            config_getter=lambda: self.config,
            password_getter=self._password,
            on_log=lambda lvl, m: self._js("onSequenceLog", {"level": lvl, "message": m}),
        )

    # ---------------- служебное ----------------

    def _password(self) -> str:
        if self._password_mem:
            return self._password_mem
        return cfg_mod.load_smtp_password(self.config.smtp.username) or ""

    def _js(self, fn: str, payload) -> None:
        """Отправить событие в UI: через emit-hook (web) или в окно (desktop)."""
        if self.emit is not None:
            try:
                self.emit(fn, payload)
            except Exception:
                pass
            return
        if self.window is not None:
            try:
                self.window.evaluate_js(
                    f"window.{fn}({json.dumps(payload, ensure_ascii=False)})")
            except Exception:
                pass

    # ---------------- конфигурация ----------------

    def get_config(self) -> dict:
        data = self.config.to_dict()
        data["has_password"] = bool(self._password())
        return data

    def save_config(self, data: dict) -> dict:
        pw = data.pop("password", None)
        self.config = cfg_mod.AppConfig.from_dict(data)
        self.config.save()
        if pw:
            self._password_mem = pw
            cfg_mod.save_smtp_password(self.config.smtp.username, pw)
        return {"ok": True}

    def test_smtp(self) -> dict:
        ok, msg = smtp_test(self.config.smtp, self.config.sender, self._password())
        return {"ok": ok, "message": msg}

    def test_imap(self) -> dict:
        ok, msg = imap_test(self.config.imap, self._password())
        return {"ok": ok, "message": msg}

    def check_deliverability(self) -> dict:
        from . import deliverability
        email = self.config.sender.from_email or self.config.smtp.username
        domain = email.split("@")[-1] if "@" in email else ""
        if not domain:
            return {"ok": False, "message": "Сначала укажите email отправителя"}
        return deliverability.check_domain(domain)

    # ---------------- контакты / доска ----------------

    def board_data(self) -> dict:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=FOLLOWUP_DAYS)).isoformat()
        cols = []
        followups = 0
        for status, title in BOARD_COLUMNS:
            rows = self.storage.list_contacts(status=status)
            cards = [self._card(r, cutoff) for r in rows[:200]]
            followups += sum(1 for c in cards if c["followup"])
            cols.append({
                "status": status,
                "title": title,
                "count": len(rows),
                "cards": cards,
            })
        return {"columns": cols, "followups": followups, "followup_days": FOLLOWUP_DAYS}

    def _card(self, row, cutoff: str | None = None) -> dict:
        company = row["company"] or ""
        name = (f"{row['first_name']} {row['last_name']}".strip()
                or row["email"].split("@")[0])
        last_sent = self._get(row, "last_sent_at")
        needs_fu = bool(row["status"] == "sent" and last_sent and cutoff
                        and last_sent <= cutoff)
        return {
            "id": row["id"],
            "email": row["email"],
            "name": name,
            "company": company,
            "status": row["status"],
            "source": row["source"] or "",
            "reply": self._get(row, "last_reply_snippet") or "",
            "followup": needs_fu,
        }

    @staticmethod
    def _get(row, key, default=""):
        try:
            v = row[key]
            return v if v is not None else default
        except (KeyError, IndexError):
            return default

    def list_contacts(self) -> list:
        return [self._card(r) for r in self.storage.list_contacts()]

    def contacts_summary(self) -> dict:
        return {
            "total": self.storage.count_contacts(),
            "active": self.storage.count_contacts(status="active"),
            "suppressed": len(self.storage.suppressed_set()),
        }

    def add_contact(self, email: str, first_name="", last_name="", company="") -> dict:
        norm = contacts_mod.normalize_email(email)
        if not norm:
            return {"ok": False, "message": "Некорректный email"}
        if self.storage.is_suppressed(norm):
            return {"ok": False, "message": "Адрес в стоп-листе"}
        self.storage.upsert_contact(norm, first_name=first_name,
                                    last_name=last_name, company=company, source="manual")
        return {"ok": True}

    def delete_contact(self, contact_id: int) -> dict:
        self.storage.delete_contact(int(contact_id))
        return {"ok": True}

    # ---------------- поиск адреса на сайте компании ----------------

    def find_site_emails(self, url: str) -> dict:
        from . import finder
        return finder.find_site_emails(url)

    def find_sites(self, urls) -> dict:
        """Поиск по нескольким сайтам сразу (до 10)."""
        from . import finder
        return finder.find_many_sites(urls)

    def generate_role_emails(self, domains, roles=None) -> dict:
        """Сгенерировать типовые (ролевые) адреса по доменам: info@, sales@…"""
        from . import finder
        return finder.generate_role_emails(domains, roles)

    def add_found_emails(self, emails: list, company: str = "") -> dict:
        """Добавить выбранные найденные адреса в контакты (с ручного выбора)."""
        added = skipped = invalid = 0
        source = f"site:{company}" if company else "site"
        for raw in emails or []:
            norm = contacts_mod.normalize_email(raw)
            if not norm:
                invalid += 1
                continue
            if self.storage.is_suppressed(norm):
                skipped += 1
                continue
            _id, is_new = self.storage.upsert_contact(norm, company=company, source=source)
            if is_new:
                added += 1
            else:
                skipped += 1
        return {"ok": True, "added": added, "skipped": skipped, "invalid": invalid}

    def set_status(self, email: str, status: str) -> dict:
        self.storage.set_contact_status(email, status)
        return {"ok": True}

    def contact_detail(self, contact_id: int) -> dict:
        row = self.storage.get_contact(int(contact_id))
        if row is None:
            return {"ok": False, "message": "Контакт не найден"}
        acts = self.storage.list_activity(int(contact_id))
        return {
            "ok": True,
            "contact": {
                "id": row["id"],
                "email": row["email"],
                "name": (f"{row['first_name']} {row['last_name']}".strip()
                         or row["email"].split("@")[0]),
                "company": row["company"] or "",
                "status": row["status"],
                "notes": self._get(row, "notes"),
                "last_sent_at": self._get(row, "last_sent_at"),
                "last_reply_at": self._get(row, "last_reply_at"),
                "last_reply_snippet": self._get(row, "last_reply_snippet"),
                "source": row["source"] or "",
            },
            "activity": [{
                "kind": a["kind"], "detail": a["detail"],
                "created_at": a["created_at"],
            } for a in acts],
        }

    def save_notes(self, contact_id: int, notes: str) -> dict:
        self.storage.set_notes(int(contact_id), notes)
        return {"ok": True}

    def move_card(self, email: str, status: str) -> dict:
        """Перенос карточки между колонками доски."""
        if status == "unsubscribed":
            self.storage.add_suppression(email, reason="manual")
        else:
            self.storage.set_contact_status(email, status)
        return {"ok": True}

    # ---------------- стоп-лист ----------------

    def suppress(self, email: str) -> dict:
        self.storage.add_suppression(email, reason="manual")
        return {"ok": True}

    def list_suppression(self) -> list:
        return [_row_to_dict(r) for r in self.storage.list_suppression()]

    def remove_suppression(self, email: str) -> dict:
        self.storage.remove_suppression(email)
        return {"ok": True}

    # ---------------- импорт ----------------

    def preview_import(self, path: str) -> dict:
        """Прочитать файл по пути и вернуть заголовки, автомаппинг и превью.

        Используется и десктопным диалогом, и веб-загрузкой (сервер сохраняет
        загруженный файл во временный путь и передаёт его сюда).
        """
        try:
            headers, rows = contacts_mod.read_table(path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        if not rows:
            return {"ok": False, "message": "В файле нет строк с данными"}
        return {
            "ok": True,
            "path": path,
            "headers": headers,
            "guess": contacts_mod.guess_mapping(headers),
            "preview": rows[:5],
            "total_rows": len(rows),
        }

    def pick_import_file(self) -> dict:
        """Desktop: открыть нативный диалог выбора файла и вернуть превью."""
        if self.window is None:
            return {"ok": False, "message": "Окно не готово"}
        import webview
        types = ("Списки (*.csv;*.tsv;*.txt;*.xlsx;*.xlsm)", "Все файлы (*.*)")
        result = self.window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        if not result:
            return {"ok": False, "cancelled": True}
        return self.preview_import(result[0])

    def run_import(self, path: str, mapping: dict) -> dict:
        try:
            headers, rows = contacts_mod.read_table(path)
            mapping = {k: int(v) for k, v in mapping.items() if v is not None and int(v) >= 0}
            result = contacts_mod.import_rows(self.storage, headers, rows, mapping, source=path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        return {
            "ok": True,
            "imported": result.imported,
            "updated": result.updated,
            "duplicates": result.duplicates,
            "invalid": result.invalid,
            "suppressed": result.suppressed,
            "summary": result.summary(),
        }

    # ---------------- шаблоны писем ----------------

    def list_templates(self) -> list:
        return [_row_to_dict(r) for r in self.storage.list_templates()]

    def save_template(self, name, subject, body_text="", body_html="", template_id=None) -> dict:
        if not (name or "").strip():
            return {"ok": False, "message": "Укажите название шаблона"}
        tid = self.storage.save_template(name, subject, body_text, body_html, template_id)
        return {"ok": True, "id": tid}

    def delete_template(self, template_id) -> dict:
        self.storage.delete_template(template_id)
        return {"ok": True}

    # ---------------- письмо / превью ----------------

    def preview_message(self, subject: str, body_text: str, body_html: str,
                        contact_id=None) -> dict:
        contact = None
        if contact_id is not None:
            contact = next((c for c in self.storage.list_contacts()
                            if c["id"] == int(contact_id)), None)
        if contact is None:
            contact = {"email": "example@company.ru", "first_name": "Иван",
                       "last_name": "Петров", "company": "Компания", "fields_json": "{}"}
        camp = {"subject": subject, "body_text": body_text, "body_html": body_html}
        subj, text, html = templates.render_message(camp, contact, self.config.sender)
        return {"subject": subj, "text": text, "html": html}

    def send_test(self, subject: str, body_text: str, body_html: str) -> dict:
        err = self._preflight()
        if err:
            return {"ok": False, "message": err}
        to = self.config.sender.from_email or self.config.smtp.username
        contact = {"email": to, "first_name": "Тест", "last_name": "",
                   "company": "Тест", "fields_json": "{}"}
        camp = {"subject": subject, "body_text": body_text, "body_html": body_html}
        subj, text, html = templates.render_message(camp, contact, self.config.sender)
        try:
            sender = SmtpSender(self.config.smtp, self.config.sender, self._password())
            sender.connect()
            sender.send_simple(to, subj or "(без темы)", text, html)
            sender.close()
            return {"ok": True, "message": f"Тестовое письмо отправлено на {to}"}
        except SmtpError as e:
            return {"ok": False, "message": str(e)}

    def _preflight(self) -> str | None:
        c = self.config
        if not c.smtp.host or not c.smtp.username:
            return "Не настроен SMTP (раздел «Настройки»)"
        if not self._password():
            return "Не введён пароль SMTP"
        if not (c.sender.from_email or c.smtp.username):
            return "Не указан email отправителя"
        return None

    # ---------------- рассылка ----------------

    def start_campaign(self, name: str, subject: str, body_text: str, body_html: str) -> dict:
        if self.runner and self.runner.is_running():
            return {"ok": False, "message": "Рассылка уже идёт"}
        err = self._preflight()
        if err:
            return {"ok": False, "message": err}
        if not subject.strip():
            return {"ok": False, "message": "Укажите тему письма"}
        if not body_text.strip() and not body_html.strip():
            return {"ok": False, "message": "Письмо пустое"}
        if self.storage.count_contacts(status="active") == 0:
            return {"ok": False, "message": "Нет активных получателей"}

        self.campaign_id = self.storage.create_campaign(
            name or f"Кампания {subject[:40]}", subject, body_text, body_html)
        self.runner = CampaignRunner(
            self.storage, self.config, self._password(),
            on_progress=lambda p: self._js("onCampaignProgress", {
                "total": p.total, "sent": p.sent, "failed": p.failed,
                "skipped": p.skipped, "message": p.message,
                "current": p.current_email, "finished": p.finished,
            }),
            on_log=lambda lvl, m: self._js("onCampaignLog", {"level": lvl, "message": m}),
        )
        self.runner.start(self.campaign_id)
        return {"ok": True, "recipients": self.storage.count_contacts(status="active")}

    def stop_campaign(self) -> dict:
        if self.runner:
            self.runner.stop()
        return {"ok": True}

    # ---------------- ответы (IMAP) ----------------

    def fetch_replies(self) -> dict:
        if not self.config.imap.host:
            return {"ok": False, "message": "IMAP не настроен"}
        try:
            with ImapReader(self.config.imap, self._password()) as reader:
                replies = reader.fetch_recent(limit=50)
        except ImapError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        return {"ok": True, "replies": [{
            "from_email": r.from_email, "from_name": r.from_name,
            "subject": r.subject, "snippet": r.snippet,
            "date": r.date.strftime("%Y-%m-%d %H:%M") if r.date else "",
            "is_unsubscribe": r.is_unsubscribe,
        } for r in replies]}

    def sync_unsubscribes(self) -> dict:
        if not self.config.imap.host:
            return {"ok": False, "message": "IMAP не настроен"}
        try:
            added = sync_unsubscribes(self.storage, self.config.imap, self._password())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        return {"ok": True, "added": added}

    def sync_replies(self) -> dict:
        """Связать входящие ответы с контактами: ответившие → «Ответили»,
        отказавшиеся → стоп-лист."""
        if not self.config.imap.host:
            return {"ok": False, "message": "IMAP не настроен"}
        try:
            res = sync_replies(self.storage, self.config.imap, self._password())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        return {"ok": True, **res}

    # ---------------- цепочки писем (sequences) ----------------

    def start_scheduler(self) -> None:
        """Запустить фоновый планировщик цепочек (идемпотентно)."""
        self.scheduler.start()

    def list_sequences(self) -> list:
        out = []
        for s in self.storage.list_sequences():
            st = self.storage.sequence_stats(s["id"])
            out.append({
                "id": s["id"], "name": s["name"], "status": s["status"],
                "steps": self.storage.count_steps(s["id"]),
                "created_at": s["created_at"],
                "stats": st,
            })
        return out

    def get_sequence(self, sequence_id: int) -> dict:
        s = self.storage.get_sequence(int(sequence_id))
        if s is None:
            return {"ok": False, "message": "Цепочка не найдена"}
        steps = [{
            "step_order": st["step_order"],
            "delay_days": st["delay_days"],
            "subject": st["subject"],
            "body_text": st["body_text"],
            "body_html": st["body_html"],
        } for st in self.storage.list_steps(int(sequence_id))]
        return {
            "ok": True,
            "sequence": {"id": s["id"], "name": s["name"], "status": s["status"]},
            "steps": steps,
            "stats": self.storage.sequence_stats(int(sequence_id)),
        }

    def save_sequence(self, name: str, steps: list, sequence_id=None) -> dict:
        if not (name or "").strip():
            return {"ok": False, "message": "Укажите название цепочки"}
        steps = steps or []
        if not steps:
            return {"ok": False, "message": "Добавьте хотя бы один шаг"}
        for i, s in enumerate(steps, 1):
            if not (s.get("subject") or "").strip():
                return {"ok": False, "message": f"Шаг {i}: укажите тему письма"}
            if not (s.get("body_text") or "").strip() and not (s.get("body_html") or "").strip():
                return {"ok": False, "message": f"Шаг {i}: письмо пустое"}
        if sequence_id:
            sid = int(sequence_id)
            self.storage.update_sequence(sid, name=name)
        else:
            sid = self.storage.create_sequence(name)
        self.storage.replace_steps(sid, steps)
        return {"ok": True, "id": sid}

    def delete_sequence(self, sequence_id: int) -> dict:
        self.storage.delete_sequence(int(sequence_id))
        return {"ok": True}

    def set_sequence_status(self, sequence_id: int, status: str) -> dict:
        if status not in ("active", "paused", "archived"):
            return {"ok": False, "message": "Недопустимый статус"}
        self.storage.set_sequence_status(int(sequence_id), status)
        return {"ok": True}

    def enroll_active(self, sequence_id: int) -> dict:
        """Добавить всех активных контактов (не в стоп-листе) в цепочку.

        Уже добавленные (активная запись) пропускаются. Первый шаг ставится
        в очередь с учётом его задержки (обычно 0 — уйдёт при ближайшем
        проходе планировщика).
        """
        sid = int(sequence_id)
        seq = self.storage.get_sequence(sid)
        if seq is None:
            return {"ok": False, "message": "Цепочка не найдена"}
        steps = self.storage.list_steps(sid)
        if not steps:
            return {"ok": False, "message": "В цепочке нет шагов"}

        first_delay = float(steps[0]["delay_days"] or 0)
        next_run = (datetime.now(timezone.utc)
                    + timedelta(days=first_delay)).isoformat()

        already = self.storage.active_enrollment_emails(sid)
        suppressed = self.storage.suppressed_set()
        added = skipped = 0
        for c in self.storage.list_contacts(status="active"):
            email = c["email"]
            if email in already or email in suppressed:
                skipped += 1
                continue
            self.storage.enroll_contact(sid, c["id"], email, next_run)
            added += 1
        # цепочка должна быть активной, чтобы планировщик её вёл
        if seq["status"] != "active" and added:
            self.storage.set_sequence_status(sid, "active")
        return {"ok": True, "added": added, "skipped": skipped}

    def sequence_stats(self, sequence_id: int) -> dict:
        return self.storage.sequence_stats(int(sequence_id))

    # ---------------- жизненный цикл ----------------

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        if self.runner and self.runner.is_running():
            self.runner.stop()
            self.runner.join(timeout=5)
        self.storage.close()
