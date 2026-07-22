"""Вкладка «Письмо и рассылка»: редактор, превью, отправка с прогрессом."""

from __future__ import annotations

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

from .. import templates
from ..campaign import CampaignRunner


class _Bridge(QObject):
    """Мостик из фонового потока рассылки в UI-поток через сигналы Qt."""
    progress = Signal(object)
    log = Signal(str, str)


class CampaignTab(QWidget):
    def __init__(self, storage, config, get_password, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.config = config
        self.get_password = get_password  # callable -> str
        self.runner: CampaignRunner | None = None
        self.campaign_id: int | None = None
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.log.connect(self._append_log)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # --- слева: редактор ---
        editor = QWidget()
        el = QVBoxLayout(editor)

        el.addWidget(QLabel("Тема письма:"))
        self.subject = QLineEdit()
        self.subject.setPlaceholderText("Например: {{company}} — предложение о сотрудничестве")
        el.addWidget(self.subject)

        el.addWidget(QLabel("Текст письма (plain text):"))
        self.body_text = QPlainTextEdit()
        self.body_text.setPlaceholderText(
            "Здравствуйте, {{first_name|коллеги}}!\n\n"
            "Пишу по поводу…\n\n"
            "Плейсхолдеры: {{first_name}}, {{last_name}}, {{company}}, {{email}}\n"
            "С дефолтом: {{first_name|коллеги}}")
        el.addWidget(self.body_text, 1)

        el.addWidget(QLabel("HTML-версия (необязательно):"))
        self.body_html = QPlainTextEdit()
        self.body_html.setPlaceholderText("<p>Здравствуйте, {{first_name|коллеги}}!</p>")
        self.body_html.setMaximumHeight(120)
        el.addWidget(self.body_html)

        hint = QLabel("Подвал с реквизитами и ссылкой на отписку добавляется автоматически.")
        hint.setStyleSheet("color:#888;font-size:11px"); hint.setWordWrap(True)
        el.addWidget(hint)
        splitter.addWidget(editor)

        # --- справа: превью + управление ---
        right = QWidget()
        rl = QVBoxLayout(right)

        prev_box = QGroupBox("Превью на контакте")
        pv = QVBoxLayout(prev_box)
        row = QHBoxLayout()
        self.preview_pick = QComboBox()
        btn_prev = QPushButton("Обновить превью")
        btn_prev.clicked.connect(self._update_preview)
        row.addWidget(QLabel("Контакт:")); row.addWidget(self.preview_pick, 1)
        row.addWidget(btn_prev)
        pv.addLayout(row)
        self.preview = QTextBrowser()
        pv.addWidget(self.preview, 1)
        rl.addWidget(prev_box, 1)

        # --- отправка ---
        send_box = QGroupBox("Рассылка")
        sv = QVBoxLayout(send_box)
        self.recipients_lbl = QLabel()
        sv.addWidget(self.recipients_lbl)
        btn_row = QHBoxLayout()
        self.btn_test_send = QPushButton("Тестовое письмо себе")
        self.btn_test_send.clicked.connect(self._send_test)
        self.btn_start = QPushButton("Начать рассылку")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("Стоп")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_test_send)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        sv.addLayout(btn_row)
        self.progress = QProgressBar()
        sv.addWidget(self.progress)
        self.status_lbl = QLabel("Готово.")
        sv.addWidget(self.status_lbl)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(160)
        sv.addWidget(self.log)
        rl.addWidget(send_box)

        splitter.addWidget(right)
        splitter.setSizes([500, 500])

    # ---------------- обновление при показе ----------------

    def refresh(self):
        active = self.storage.list_contacts(status="active")
        self.preview_pick.clear()
        for c in active[:500]:
            label = c["email"] + (f" ({c['first_name']})" if c["first_name"] else "")
            self.preview_pick.addItem(label, c["id"])
        supp = len(self.storage.suppressed_set())
        self.recipients_lbl.setText(
            f"Активных получателей: {len(active)}   (в стоп-листе: {supp})")

    # ---------------- превью ----------------

    def _current_campaign_obj(self):
        return {
            "subject": self.subject.text(),
            "body_text": self.body_text.toPlainText(),
            "body_html": self.body_html.toPlainText(),
        }

    def _update_preview(self):
        cid = self.preview_pick.currentData()
        if cid is None:
            self.preview.setPlainText("Нет контактов для превью.")
            return
        contact = next((c for c in self.storage.list_contacts() if c["id"] == cid), None)
        if not contact:
            return
        subj, text, html = templates.render_message(
            self._current_campaign_obj(), contact, self.config.sender)
        if html:
            self.preview.setHtml(f"<b>Тема:</b> {subj}<hr>{html}")
        else:
            self.preview.setPlainText(f"Тема: {subj}\n\n{text}")

    # ---------------- сохранение кампании ----------------

    def _persist_campaign(self) -> int | None:
        subject = self.subject.text().strip()
        if not subject:
            QMessageBox.warning(self, "Письмо", "Укажите тему письма.")
            return None
        if not self.body_text.toPlainText().strip() and not self.body_html.toPlainText().strip():
            QMessageBox.warning(self, "Письмо", "Письмо пустое.")
            return None
        name = f"Кампания {subject[:40]}"
        if self.campaign_id is None:
            self.campaign_id = self.storage.create_campaign(
                name, subject, self.body_text.toPlainText(), self.body_html.toPlainText())
        else:
            self.storage.update_campaign(
                self.campaign_id, name=name, subject=subject,
                body_text=self.body_text.toPlainText(),
                body_html=self.body_html.toPlainText())
        return self.campaign_id

    # ---------------- предполётная проверка ----------------

    def _preflight(self) -> str | None:
        c = self.config
        if not c.smtp.host or not c.smtp.username:
            return "Не настроен SMTP (вкладка «Настройки»)."
        if not self.get_password():
            return "Не введён пароль SMTP (вкладка «Настройки»)."
        if not c.sender.from_email and not c.smtp.username:
            return "Не указан email отправителя."
        return None

    # ---------------- тестовое письмо ----------------

    def _send_test(self):
        err = self._preflight()
        if err:
            QMessageBox.warning(self, "Проверка", err)
            return
        to = self.config.sender.from_email or self.config.smtp.username
        contact = {"email": to, "first_name": "Тест", "last_name": "",
                   "company": "Тест", "fields_json": "{}"}
        subj, text, html = templates.render_message(
            self._current_campaign_obj(), contact, self.config.sender)
        from ..smtp_client import SmtpSender, SmtpError
        try:
            sender = SmtpSender(self.config.smtp, self.config.sender, self.get_password())
            sender.connect()
            sender.send_simple(to, subj or "(без темы)", text, html)
            sender.close()
            QMessageBox.information(self, "Тест", f"Тестовое письмо отправлено на {to}.")
        except SmtpError as e:
            QMessageBox.critical(self, "Тест", str(e))

    # ---------------- рассылка ----------------

    def _start(self):
        err = self._preflight()
        if err:
            QMessageBox.warning(self, "Проверка", err)
            return
        cid = self._persist_campaign()
        if cid is None:
            return
        active = self.storage.count_contacts(status="active")
        if active == 0:
            QMessageBox.information(self, "Рассылка", "Нет активных получателей.")
            return
        if QMessageBox.question(
                self, "Подтверждение",
                f"Отправить письмо {active} получателям?\n\n"
                f"Темп: {self.config.limits.per_hour}/час, "
                f"пауза {self.config.limits.delay_seconds} c между письмами.\n"
                "Убедитесь, что у всех получателей есть согласие на рассылку."
        ) != QMessageBox.Yes:
            return

        self.log.clear()
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_test_send.setEnabled(False)

        self.runner = CampaignRunner(
            self.storage, self.config, self.get_password(),
            on_progress=lambda p: self._bridge.progress.emit(p),
            on_log=lambda lvl, m: self._bridge.log.emit(lvl, m),
        )
        self.runner.start(cid)

    def _stop(self):
        if self.runner:
            self.runner.stop()
            self.status_lbl.setText("Останавливаю…")

    def _on_progress(self, p):
        if p.total:
            done = p.sent + p.failed + p.skipped
            self.progress.setMaximum(p.total)
            self.progress.setValue(done)
        self.status_lbl.setText(
            f"{p.message}   ✓{p.sent}  ✗{p.failed}  ⊘{p.skipped}")
        if p.finished:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_test_send.setEnabled(True)
            # завершённую кампанию считаем закрытой — следующий запуск = новая
            self.campaign_id = None

    def _append_log(self, level, message):
        prefix = {"error": "[ОШИБКА] ", "warn": "[!] ", "info": ""}.get(level, "")
        self.log.appendPlainText(prefix + message)
