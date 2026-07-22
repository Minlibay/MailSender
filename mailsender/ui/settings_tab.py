"""Вкладка «Настройки»: SMTP, IMAP, отправитель, лимиты + тест соединения."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .. import config as cfg_mod
from ..imap_client import test_connection as imap_test
from ..smtp_client import test_connection as smtp_test


class _TestWorker(QThread):
    """Тест соединения в отдельном потоке, чтобы не морозить UI."""
    done = Signal(bool, str)

    def __init__(self, kind, *args):
        super().__init__()
        self.kind = kind
        self.args = args

    def run(self):
        try:
            if self.kind == "smtp":
                ok, msg = smtp_test(*self.args)
            else:
                ok, msg = imap_test(*self.args)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        self.done.emit(ok, msg)


class SettingsTab(QWidget):
    config_saved = Signal()

    def __init__(self, config: cfg_mod.AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self._worker: _TestWorker | None = None
        self._build()
        self._load_into_form()

    # ---------------- построение формы ----------------

    def _build(self):
        root = QVBoxLayout(self)

        # --- SMTP ---
        smtp_box = QGroupBox("Отправка (SMTP)")
        smtp_form = QFormLayout(smtp_box)
        self.smtp_host = QLineEdit()
        self.smtp_host.setPlaceholderText("smtp.company.ru")
        self.smtp_port = QSpinBox(); self.smtp_port.setRange(1, 65535); self.smtp_port.setValue(587)
        self.smtp_user = QLineEdit()
        self.smtp_pass = QLineEdit(); self.smtp_pass.setEchoMode(QLineEdit.Password)
        self.smtp_pass.setPlaceholderText("хранится в системном хранилище, не в файле")
        self.smtp_tls = QCheckBox("STARTTLS (порт 587)"); self.smtp_tls.setChecked(True)
        self.smtp_ssl = QCheckBox("SSL/TLS (порт 465)")
        self.smtp_tls.toggled.connect(lambda v: v and self.smtp_ssl.setChecked(False))
        self.smtp_ssl.toggled.connect(lambda v: v and self.smtp_tls.setChecked(False))
        smtp_form.addRow("Хост:", self.smtp_host)
        smtp_form.addRow("Порт:", self.smtp_port)
        smtp_form.addRow("Логин:", self.smtp_user)
        smtp_form.addRow("Пароль:", self.smtp_pass)
        smtp_form.addRow("", self.smtp_tls)
        smtp_form.addRow("", self.smtp_ssl)
        self.btn_test_smtp = QPushButton("Проверить SMTP")
        self.btn_test_smtp.clicked.connect(self._test_smtp)
        smtp_form.addRow("", self.btn_test_smtp)
        root.addWidget(smtp_box)

        # --- IMAP ---
        imap_box = QGroupBox("Чтение ответов (IMAP) — опционально")
        imap_form = QFormLayout(imap_box)
        self.imap_host = QLineEdit(); self.imap_host.setPlaceholderText("imap.company.ru")
        self.imap_port = QSpinBox(); self.imap_port.setRange(1, 65535); self.imap_port.setValue(993)
        self.imap_user = QLineEdit()
        self.imap_ssl = QCheckBox("SSL/TLS"); self.imap_ssl.setChecked(True)
        imap_form.addRow("Хост:", self.imap_host)
        imap_form.addRow("Порт:", self.imap_port)
        imap_form.addRow("Логин:", self.imap_user)
        imap_form.addRow("", self.imap_ssl)
        self.btn_test_imap = QPushButton("Проверить IMAP")
        self.btn_test_imap.clicked.connect(self._test_imap)
        imap_form.addRow("", self.btn_test_imap)
        root.addWidget(imap_box)

        # --- Отправитель / compliance ---
        sender_box = QGroupBox("Отправитель и обязательные реквизиты")
        sender_form = QFormLayout(sender_box)
        self.from_name = QLineEdit()
        self.from_email = QLineEdit(); self.from_email.setPlaceholderText("sales@company.ru")
        self.reply_to = QLineEdit()
        self.org_name = QLineEdit()
        self.postal = QLineEdit(); self.postal.setPlaceholderText("Адрес — добавляется подписью, если заполнен")
        sender_form.addRow("Имя отправителя:", self.from_name)
        sender_form.addRow("Email отправителя:", self.from_email)
        sender_form.addRow("Reply-To:", self.reply_to)
        sender_form.addRow("Организация (подпись):", self.org_name)
        sender_form.addRow("Адрес (подпись):", self.postal)
        hint = QLabel("Каждому уходит одно персональное письмо с рабочего ящика. "
                      "Организация/адрес добавляются подписью, только если заполнены.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#888;font-size:11px")
        sender_form.addRow(hint)
        root.addWidget(sender_box)

        # --- Лимиты ---
        limits_box = QGroupBox("Лимиты и темп рассылки")
        lf = QFormLayout(limits_box)
        self.per_hour = QSpinBox(); self.per_hour.setRange(1, 100000); self.per_hour.setValue(100)
        self.per_day = QSpinBox(); self.per_day.setRange(1, 1000000); self.per_day.setValue(500)
        self.delay = QDoubleSpinBox(); self.delay.setRange(0, 3600); self.delay.setValue(3.0); self.delay.setSuffix(" c")
        self.batch = QSpinBox(); self.batch.setRange(0, 100000); self.batch.setValue(50)
        self.batch_pause = QDoubleSpinBox(); self.batch_pause.setRange(0, 86400); self.batch_pause.setValue(60.0); self.batch_pause.setSuffix(" c")
        lf.addRow("Писем в час:", self.per_hour)
        lf.addRow("Писем в сутки:", self.per_day)
        lf.addRow("Пауза между письмами:", self.delay)
        lf.addRow("Размер пачки:", self.batch)
        lf.addRow("Пауза между пачками:", self.batch_pause)
        root.addWidget(limits_box)

        # --- Сохранить ---
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_save = QPushButton("Сохранить настройки")
        self.btn_save.clicked.connect(self._save)
        btn_row.addWidget(self.btn_save)
        root.addLayout(btn_row)
        root.addStretch()

    # ---------------- загрузка/сохранение ----------------

    def _load_into_form(self):
        c = self.config
        self.smtp_host.setText(c.smtp.host)
        self.smtp_port.setValue(c.smtp.port)
        self.smtp_user.setText(c.smtp.username)
        self.smtp_tls.setChecked(c.smtp.use_tls)
        self.smtp_ssl.setChecked(c.smtp.use_ssl)
        pw = cfg_mod.load_smtp_password(c.smtp.username)
        if pw:
            self.smtp_pass.setText(pw)

        self.imap_host.setText(c.imap.host)
        self.imap_port.setValue(c.imap.port)
        self.imap_user.setText(c.imap.username)
        self.imap_ssl.setChecked(c.imap.use_ssl)

        self.from_name.setText(c.sender.from_name)
        self.from_email.setText(c.sender.from_email)
        self.reply_to.setText(c.sender.reply_to)
        self.org_name.setText(c.sender.org_name)
        self.postal.setText(c.sender.postal_address)

        self.per_hour.setValue(c.limits.per_hour)
        self.per_day.setValue(c.limits.per_day)
        self.delay.setValue(c.limits.delay_seconds)
        self.batch.setValue(c.limits.batch_size)
        self.batch_pause.setValue(c.limits.batch_pause_seconds)

    def _collect(self):
        c = self.config
        c.smtp.host = self.smtp_host.text().strip()
        c.smtp.port = self.smtp_port.value()
        c.smtp.username = self.smtp_user.text().strip()
        c.smtp.use_tls = self.smtp_tls.isChecked()
        c.smtp.use_ssl = self.smtp_ssl.isChecked()

        c.imap.host = self.imap_host.text().strip()
        c.imap.port = self.imap_port.value()
        c.imap.username = self.imap_user.text().strip()
        c.imap.use_ssl = self.imap_ssl.isChecked()

        c.sender.from_name = self.from_name.text().strip()
        c.sender.from_email = self.from_email.text().strip()
        c.sender.reply_to = self.reply_to.text().strip()
        c.sender.org_name = self.org_name.text().strip()
        c.sender.postal_address = self.postal.text().strip()

        c.limits.per_hour = self.per_hour.value()
        c.limits.per_day = self.per_day.value()
        c.limits.delay_seconds = self.delay.value()
        c.limits.batch_size = self.batch.value()
        c.limits.batch_pause_seconds = self.batch_pause.value()

    def current_password(self) -> str:
        return self.smtp_pass.text()

    def _save(self):
        self._collect()
        self.config.save()
        pw = self.smtp_pass.text()
        if pw and self.config.smtp.username:
            saved = cfg_mod.save_smtp_password(self.config.smtp.username, pw)
            if not saved:
                QMessageBox.warning(
                    self, "Пароль",
                    "Не удалось сохранить пароль в системном хранилище — "
                    "он будет действовать только до закрытия программы.",
                )
        self.config_saved.emit()
        QMessageBox.information(self, "Настройки", "Настройки сохранены.")

    # ---------------- тесты ----------------

    def _test_smtp(self):
        self._collect()
        pw = self.smtp_pass.text()
        if not self.config.smtp.host:
            QMessageBox.warning(self, "SMTP", "Укажите хост SMTP.")
            return
        self.btn_test_smtp.setEnabled(False)
        self.btn_test_smtp.setText("Проверка…")
        self._worker = _TestWorker("smtp", self.config.smtp, self.config.sender, pw)
        self._worker.done.connect(self._on_smtp_tested)
        self._worker.start()

    def _on_smtp_tested(self, ok, msg):
        self.btn_test_smtp.setEnabled(True)
        self.btn_test_smtp.setText("Проверить SMTP")
        (QMessageBox.information if ok else QMessageBox.critical)(self, "SMTP", msg)

    def _test_imap(self):
        self._collect()
        pw = self.smtp_pass.text()  # обычно тот же пароль
        if not self.config.imap.host:
            QMessageBox.warning(self, "IMAP", "Укажите хост IMAP.")
            return
        self.btn_test_imap.setEnabled(False)
        self.btn_test_imap.setText("Проверка…")
        self._worker = _TestWorker("imap", self.config.imap, pw)
        self._worker.done.connect(self._on_imap_tested)
        self._worker.start()

    def _on_imap_tested(self, ok, msg):
        self.btn_test_imap.setEnabled(True)
        self.btn_test_imap.setText("Проверить IMAP")
        (QMessageBox.information if ok else QMessageBox.critical)(self, "IMAP", msg)
