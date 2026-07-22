"""Движок рассылки: аккуратная отправка по своей базе.

Ключевые гарантии «белой» рассылки, зашитые в код:
  * адреса из стоп-листа и со статусом unsubscribed/bounced не получают писем;
  * соблюдаются лимиты в час/сутки и паузы между письмами/пачками;
  * каждое письмо пишется в лог доставки (sent/failed/skipped);
  * повторный запуск кампании не шлёт повторно тем, кому уже ушло.

Движок работает в отдельном потоке и репортит прогресс через callback,
чтобы не блокировать GUI. Останавливается кооперативно через stop().
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import templates
from .smtp_client import SmtpError, SmtpSender


@dataclass
class Progress:
    total: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    current_email: str = ""
    message: str = ""
    finished: bool = False


class CampaignRunner:
    """Запускает рассылку кампании в фоновом потоке."""

    def __init__(self, storage, config, password: str,
                 on_progress=None, on_log=None):
        self.storage = storage
        self.config = config
        self.password = password
        self.on_progress = on_progress or (lambda p: None)
        self.on_log = on_log or (lambda level, msg: None)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._progress = Progress()

    # ---- управление ----

    def start(self, campaign_id: int) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Рассылка уже идёт")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(campaign_id,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- внутреннее ----

    def _emit(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self._progress, k, v)
        self.on_progress(self._progress)

    def _quota_left_today(self) -> int:
        start_of_day = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        used = self.storage.sent_count_since(start_of_day.isoformat())
        return max(0, self.config.limits.per_day - used)

    def _run(self, campaign_id: int) -> None:
        limits = self.config.limits
        camp = self.storage.get_campaign(campaign_id)
        if camp is None:
            self.on_log("error", "Кампания не найдена")
            self._emit(finished=True, message="Кампания не найдена")
            return

        # выборка получателей: только активные и не в стоп-листе,
        # исключая тех, кому уже успешно ушло в этой кампании.
        contacts = self.storage.list_contacts(status="active")
        already = self.storage.already_sent_emails(campaign_id)
        suppressed = self.storage.suppressed_set()
        recipients = [c for c in contacts
                      if c["email"] not in already and c["email"] not in suppressed]

        total = len(recipients)
        self._progress = Progress(total=total, message="Старт рассылки…")
        self.on_progress(self._progress)
        self.on_log("info", f"К отправке: {total} контактов "
                            f"(в базе {len(contacts)}, уже отправлено {len(already)}, "
                            f"в стоп-листе {len(suppressed)})")

        if total == 0:
            self.storage.update_campaign(campaign_id, status="done")
            self._emit(finished=True, message="Нет получателей для отправки")
            return

        day_quota = self._quota_left_today()
        if day_quota <= 0:
            self.on_log("warn", "Исчерпан суточный лимит отправки")
            self._emit(finished=True, message="Достигнут суточный лимит")
            return

        self.storage.update_campaign(campaign_id, status="sending")
        sender_cfg = self.config.sender
        smtp = SmtpSender(self.config.smtp, sender_cfg, self.password)

        try:
            smtp.connect()
        except SmtpError as e:
            self.on_log("error", f"SMTP: {e}")
            self.storage.update_campaign(campaign_id, status="stopped")
            self._emit(finished=True, message=str(e))
            return

        hour_window_start = time.monotonic()
        hour_count = 0
        sent_in_run = 0

        try:
            for i, contact in enumerate(recipients, 1):
                if self._stop.is_set():
                    self.on_log("warn", "Рассылка остановлена пользователем")
                    self.storage.update_campaign(campaign_id, status="stopped")
                    break

                email = contact["email"]

                # суточный лимит
                if sent_in_run >= day_quota:
                    self.on_log("warn", "Достигнут суточный лимит — остановка")
                    self.storage.update_campaign(campaign_id, status="stopped")
                    break

                # часовой лимит: если превысили — ждём конца окна
                if hour_count >= limits.per_hour:
                    elapsed = time.monotonic() - hour_window_start
                    wait = max(0, 3600 - elapsed)
                    if wait > 0 and not self._stop.is_set():
                        self.on_log("info", f"Часовой лимит достигнут, пауза {int(wait)} с")
                        self._interruptible_sleep(wait)
                    hour_window_start = time.monotonic()
                    hour_count = 0
                    if self._stop.is_set():
                        continue

                # финальная защита: адрес мог попасть в стоп-лист во время рассылки
                if self.storage.is_suppressed(email):
                    self.storage.log_send(campaign_id, contact["id"], email,
                                          "skipped", "в стоп-листе")
                    self._emit(skipped=self._progress.skipped + 1, current_email=email)
                    continue

                subject, text_body, html_body = templates.render_message(
                    camp, contact, sender_cfg
                )
                if not subject.strip():
                    self.storage.log_send(campaign_id, contact["id"], email,
                                          "failed", "пустая тема")
                    self._emit(failed=self._progress.failed + 1, current_email=email)
                    continue

                try:
                    smtp.send_simple(email, subject, text_body, html_body)
                    self.storage.log_send(campaign_id, contact["id"], email, "sent")
                    # контакт уезжает в колонку «Отправлено» + запись в таймлайн
                    self.storage.mark_sent(email)
                    hour_count += 1
                    sent_in_run += 1
                    self._emit(sent=self._progress.sent + 1, current_email=email,
                               message=f"Отправлено {i}/{total}")
                except SmtpError as e:
                    self.storage.log_send(campaign_id, contact["id"], email,
                                          "failed", str(e))
                    self.on_log("error", f"{email}: {e}")
                    self._emit(failed=self._progress.failed + 1, current_email=email)

                # паузы: между письмами и длинная пауза между пачками
                if i < total and not self._stop.is_set():
                    self._interruptible_sleep(limits.delay_seconds)
                    if limits.batch_size and i % limits.batch_size == 0:
                        self.on_log("info", f"Пачка {limits.batch_size} отправлена, "
                                            f"пауза {int(limits.batch_pause_seconds)} с")
                        self._interruptible_sleep(limits.batch_pause_seconds)
        finally:
            smtp.close()

        if not self._stop.is_set():
            self.storage.update_campaign(campaign_id, status="done")
        stats = self.storage.campaign_stats(campaign_id)
        self.on_log("info", f"Готово. Отправлено: {stats['sent']}, "
                            f"ошибок: {stats['failed']}, пропущено: {stats['skipped']}")
        self._emit(finished=True, message="Рассылка завершена")

    def _interruptible_sleep(self, seconds: float) -> None:
        """Спать, но реагировать на stop() без задержки."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return
            time.sleep(min(0.2, deadline - time.monotonic()))
