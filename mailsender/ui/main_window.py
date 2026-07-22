"""Главное окно приложения: вкладки и общий доступ к настройкам/БД."""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QTabWidget

from .. import config as cfg_mod
from ..storage import Storage
from .campaign_tab import CampaignTab
from .contacts_tab import ContactsTab
from .replies_tab import RepliesTab
from .settings_tab import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MailSender — рассылка по своей базе")
        self.resize(1100, 720)

        self.config = cfg_mod.AppConfig.load()
        self.storage = Storage()

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.settings_tab = SettingsTab(self.config)
        self.contacts_tab = ContactsTab(self.storage)
        self.campaign_tab = CampaignTab(
            self.storage, self.config, self._get_password)
        self.replies_tab = RepliesTab(
            self.storage, self.config, self._get_password)

        self.tabs.addTab(self.settings_tab, "Настройки")
        self.tabs.addTab(self.contacts_tab, "Контакты")
        self.tabs.addTab(self.campaign_tab, "Письмо и рассылка")
        self.tabs.addTab(self.replies_tab, "Ответы")

        self.settings_tab.config_saved.connect(self._on_config_saved)
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _get_password(self) -> str:
        """Пароль SMTP: из поля настроек или из системного хранилища."""
        pw = self.settings_tab.current_password()
        if pw:
            return pw
        return cfg_mod.load_smtp_password(self.config.smtp.username) or ""

    def _on_config_saved(self):
        # обновим зависимые вкладки (например, лимиты/отправитель в превью)
        self.campaign_tab.refresh()

    def _on_tab_changed(self, index):
        widget = self.tabs.widget(index)
        if widget is self.contacts_tab:
            self.contacts_tab.refresh()
        elif widget is self.campaign_tab:
            self.campaign_tab.refresh()

    def closeEvent(self, event):
        if self.campaign_tab.runner and self.campaign_tab.runner.is_running():
            self.campaign_tab.runner.stop()
            self.campaign_tab.runner.join(timeout=5)
        self.storage.close()
        super().closeEvent(event)
