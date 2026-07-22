"""Вкладка «Ответы»: чтение входящих по IMAP и автосбор отписок."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..imap_client import ImapError, ImapReader, sync_unsubscribes


class _FetchWorker(QThread):
    done = Signal(object, str)   # (replies|None, error)

    def __init__(self, imap_cfg, password):
        super().__init__()
        self.imap_cfg = imap_cfg
        self.password = password

    def run(self):
        try:
            with ImapReader(self.imap_cfg, self.password) as reader:
                replies = reader.fetch_recent(limit=50)
            self.done.emit(replies, "")
        except ImapError as e:
            self.done.emit(None, str(e))
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, str(e))


class RepliesTab(QWidget):
    def __init__(self, storage, config, get_password, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.config = config
        self.get_password = get_password
        self._worker: _FetchWorker | None = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_fetch = QPushButton("Загрузить ответы")
        self.btn_fetch.clicked.connect(self._fetch)
        self.btn_sync = QPushButton("Собрать отписки в стоп-лист")
        self.btn_sync.clicked.connect(self._sync_unsub)
        self.btn_supp_sel = QPushButton("Выбранного — в стоп-лист")
        self.btn_supp_sel.clicked.connect(self._suppress_selected)
        bar.addWidget(self.btn_fetch)
        bar.addWidget(self.btn_sync)
        bar.addWidget(self.btn_supp_sel)
        bar.addStretch()
        root.addLayout(bar)

        note = QLabel("IMAP только читает почту (не удаляет). Письма со словами "
                      "«отписаться/unsubscribe/stop» подсвечиваются.")
        note.setWordWrap(True); note.setStyleSheet("color:#888")
        root.addWidget(note)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["От", "Тема", "Дата", "Текст"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        root.addWidget(self.table)

    def _check_imap(self) -> bool:
        if not self.config.imap.host:
            QMessageBox.warning(self, "IMAP", "IMAP не настроен (вкладка «Настройки»).")
            return False
        if not self.get_password():
            QMessageBox.warning(self, "IMAP", "Не введён пароль (вкладка «Настройки»).")
            return False
        return True

    def _fetch(self):
        if not self._check_imap():
            return
        self.btn_fetch.setEnabled(False)
        self.btn_fetch.setText("Загрузка…")
        self._worker = _FetchWorker(self.config.imap, self.get_password())
        self._worker.done.connect(self._on_fetched)
        self._worker.start()

    def _on_fetched(self, replies, error):
        self.btn_fetch.setEnabled(True)
        self.btn_fetch.setText("Загрузить ответы")
        if error:
            QMessageBox.critical(self, "IMAP", error)
            return
        self.table.setRowCount(len(replies))
        for r, rep in enumerate(replies):
            frm = f"{rep.from_name} <{rep.from_email}>" if rep.from_name else rep.from_email
            date = rep.date.strftime("%Y-%m-%d %H:%M") if rep.date else ""
            cells = [frm, rep.subject, date, rep.snippet]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if rep.is_unsubscribe:
                    item.setForeground(Qt.red)
                self.table.setItem(r, col, item)
            self.table.item(r, 0).setData(Qt.UserRole, rep.from_email)

    def _sync_unsub(self):
        if not self._check_imap():
            return
        try:
            added = sync_unsubscribes(self.storage, self.config.imap, self.get_password())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "IMAP", str(e))
            return
        if added:
            QMessageBox.information(
                self, "Отписки",
                f"Добавлено в стоп-лист: {len(added)}\n" + "\n".join(added[:20]))
        else:
            QMessageBox.information(self, "Отписки", "Новых отписок не найдено.")

    def _suppress_selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        emails = []
        for r in rows:
            item = self.table.item(r, 0)
            if item:
                emails.append(item.data(Qt.UserRole))
        for e in emails:
            if e:
                self.storage.add_suppression(e, reason="unsubscribed")
        if emails:
            QMessageBox.information(self, "Стоп-лист", f"Добавлено: {len(emails)}")
