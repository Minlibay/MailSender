"""Планировщик цепочек писем: автоматическая отправка шагов по задержкам.

Работает одним фоновым потоком на процесс (в web — один воркер, в desktop —
единственное окно). Периодически (по умолчанию раз в минуту) просыпается и
отправляет те шаги цепочек, у которых подошёл срок.

Гарантии, аналогичные обычной рассылке:
  * адрес из стоп-листа или ответивший/отписавшийся контакт исключается —
    цепочка для него останавливается (не досаждаем тем, кто уже откликнулся);
  * учитывается суточный лимит (общий с кампаниями — через send_log);
  * между письмами выдерживается пауза, тик ограничен по числу писем.

Конфигурация и пароль читаются на момент отправки через колбэки, поэтому
изменения настроек подхватываются без перезапуска потока.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from . import templates
from .smtp_client import SmtpError, SmtpSender

# Останавливающие статусы контакта: цепочку дальше не ведём.
_STOP_STATUSES = {"replied", "unsubscribed", "bounced"}

_MAX_PER_TICK = 100          # верхняя граница писем за один проход планировщика

# Результаты обработки одной записи.
_SENT = "sent"
_SKIP = "skip"
_ABORT = "abort"             # SMTP недоступен — прервать весь тик


class SequenceScheduler:
    def __init__(self, storage, config_getter, password_getter,
                 on_log=None, interval: float = 60.0):
        self.storage = storage
        self._config_getter = config_getter
        self._password_getter = password_getter
        self.on_log = on_log or (lambda level, msg: None)
        self.interval = interval

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- жизненный цикл ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="SequenceScheduler")
        self._thread.start()

    def stop(self, timeout: float | None = 3) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- цикл ----

    def _loop(self) -> None:
        # Небольшая задержка на старте, чтобы не конкурировать с инициализацией.
        self._interruptible_sleep(3)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — поток не должен падать
                self.on_log("error", f"Планировщик цепочек: {e}")
            self._interruptible_sleep(self.interval)

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    def tick(self) -> int:
        """Один проход планировщика. Возвращает число отправленных писем."""
        now = datetime.now(timezone.utc)
        due = self.storage.due_enrollments(now.isoformat(), limit=_MAX_PER_TICK * 2)
        if not due:
            return 0

        config = self._config_getter()
        password = self._password_getter()
        if not config.smtp.host or not config.smtp.username or not password:
            self.on_log("warn", "Цепочки: SMTP не настроен — отправка отложена")
            return 0

        quota_left = self._quota_left_today(config)
        if quota_left <= 0:
            self.on_log("warn", "Цепочки: достигнут суточный лимит — отправка отложена")
            return 0

        smtp: SmtpSender | None = None
        sent = 0
        try:
            for enr in due:
                if self._stop.is_set() or sent >= _MAX_PER_TICK or quota_left <= 0:
                    break
                smtp, result = self._process_one(enr, config, password, smtp, now)
                if result == _ABORT:
                    break
                if result == _SENT:
                    sent += 1
                    quota_left -= 1
                    self._interruptible_sleep(min(5.0, config.limits.delay_seconds or 0))
        finally:
            if smtp is not None:
                smtp.close()
        if sent:
            self.on_log("info", f"Цепочки: отправлено писем за проход: {sent}")
        return sent

    def _quota_left_today(self, config) -> int:
        start_of_day = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        used = self.storage.sent_count_since(start_of_day.isoformat())
        return max(0, config.limits.per_day - used)

    def _process_one(self, enr, config, password, smtp, now):
        """Обработать одну запись. Возвращает (smtp, результат)."""
        enr_id = enr["id"]
        seq_id = enr["sequence_id"]

        seq = self.storage.get_sequence(seq_id)
        if seq is None:
            self.storage.update_enrollment(enr_id, status="stopped")
            return smtp, _SKIP
        if seq["status"] != "active":
            # цепочка на паузе/в архиве — оставляем запись, вернёмся позже
            return smtp, _SKIP

        steps = self.storage.list_steps(seq_id)
        idx = enr["current_step"]
        if idx >= len(steps):
            self.storage.update_enrollment(enr_id, status="completed", next_run_at=None)
            return smtp, _SKIP

        email = enr["email"]
        contact = self.storage.get_contact_by_email(email)

        # стоп-условия: в стоп-листе / ответил / отписался
        if self.storage.is_suppressed(email):
            self.storage.update_enrollment(enr_id, status="stopped", next_run_at=None)
            return smtp, _SKIP
        if contact is not None and contact["status"] in _STOP_STATUSES:
            new_status = "replied" if contact["status"] == "replied" else "stopped"
            self.storage.update_enrollment(enr_id, status=new_status, next_run_at=None)
            return smtp, _SKIP

        step = steps[idx]
        camp = {"subject": step["subject"], "body_text": step["body_text"],
                "body_html": step["body_html"]}
        ctx_contact = contact if contact is not None else {
            "email": email, "first_name": "", "last_name": "",
            "company": "", "fields_json": "{}"}
        subject, text, html = templates.render_message(camp, ctx_contact, config.sender)
        if not subject.strip():
            self.storage.log_sequence_send(enr_id, seq_id, idx, email,
                                           "failed", "пустая тема шага")
            self.storage.update_enrollment(enr_id, status="failed", next_run_at=None)
            self.on_log("error", f"{email}: пустая тема шага {idx + 1} — остановлено")
            return smtp, _SKIP

        # подключаемся лениво (одно соединение на тик)
        if smtp is None:
            smtp = SmtpSender(config.smtp, config.sender, password)
            try:
                smtp.connect()
            except SmtpError as e:
                self.on_log("error", f"Цепочки: не удалось подключиться к SMTP: {e}")
                return None, _ABORT

        try:
            smtp.send_simple(email, subject, text, html)
        except SmtpError as e:
            self.storage.log_sequence_send(enr_id, seq_id, idx, email, "failed", str(e))
            self.storage.update_enrollment(enr_id, status="failed", next_run_at=None)
            self.on_log("error", f"{email}: {e} — цепочка остановлена")
            return smtp, _SKIP

        # успех: журналируем и продвигаем к следующему шагу
        self.storage.log_sequence_send(enr_id, seq_id, idx, email, "sent")
        # в общий лог доставки — для единого суточного лимита с кампаниями
        self.storage.log_send(None, enr["contact_id"], email, "sent")
        self.storage.mark_sent(email)
        if enr["contact_id"]:
            self.storage.add_activity(
                enr["contact_id"], "sent",
                f"цепочка «{seq['name']}», шаг {idx + 1}")

        next_idx = idx + 1
        if next_idx >= len(steps):
            self.storage.update_enrollment(
                enr_id, current_step=next_idx, status="completed",
                last_step_sent_at=now.isoformat(), next_run_at=None)
        else:
            delay = float(steps[next_idx]["delay_days"] or 0)
            nra = now + timedelta(days=delay)
            self.storage.update_enrollment(
                enr_id, current_step=next_idx, next_run_at=nra.isoformat(),
                last_step_sent_at=now.isoformat())
        return smtp, _SENT
