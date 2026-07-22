"""Вкладка «Контакты»: импорт списков, таблица базы, стоп-лист."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import contacts as contacts_mod


class _MappingDialog(QDialog):
    """Диалог сопоставления колонок файла со стандартными полями."""

    FIELDS = [("email", "Email *"), ("first_name", "Имя"),
              ("last_name", "Фамилия"), ("company", "Компания")]

    def __init__(self, headers, guessed, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Сопоставление колонок")
        self.headers = headers
        self.combos: dict[str, QComboBox] = {}
        form = QFormLayout(self)
        form.addRow(QLabel("Укажите, какие колонки файла куда идут:"))
        for key, label in self.FIELDS:
            combo = QComboBox()
            combo.addItem("— нет —", -1)
            for i, h in enumerate(headers):
                combo.addItem(f"{i+1}. {h}", i)
            if key in guessed:
                combo.setCurrentIndex(guessed[key] + 1)
            self.combos[key] = combo
            form.addRow(label + ":", combo)
        note = QLabel("Остальные колонки сохранятся как доп. поля для подстановки {{...}}.")
        note.setWordWrap(True); note.setStyleSheet("color:#888;font-size:11px")
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        if self.combos["email"].currentData() < 0:
            QMessageBox.warning(self, "Колонки", "Обязательно укажите колонку Email.")
            return
        self.accept()

    def mapping(self) -> dict[str, int]:
        result = {}
        for key, combo in self.combos.items():
            idx = combo.currentData()
            if idx >= 0:
                result[key] = idx
        return result


class ContactsTab(QWidget):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self._build()
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)
        tabs.addTab(self._build_contacts_page(), "База контактов")
        tabs.addTab(self._build_suppression_page(), "Стоп-лист")

    # ---------------- база ----------------

    def _build_contacts_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)

        bar = QHBoxLayout()
        btn_import = QPushButton("Импорт из файла…")
        btn_import.clicked.connect(self._import_file)
        btn_add = QPushButton("Добавить вручную…")
        btn_add.clicked.connect(self._add_manual)
        btn_del = QPushButton("Удалить выбранные")
        btn_del.clicked.connect(self._delete_selected)
        btn_supp = QPushButton("В стоп-лист")
        btn_supp.clicked.connect(self._suppress_selected)
        self.lbl_count = QLabel()
        bar.addWidget(btn_import); bar.addWidget(btn_add)
        bar.addWidget(btn_del); bar.addWidget(btn_supp)
        bar.addStretch(); bar.addWidget(self.lbl_count)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Email", "Имя", "Фамилия", "Компания", "Статус"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnHidden(0, True)
        lay.addWidget(self.table)
        return page

    def _build_suppression_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        note = QLabel("Адреса из стоп-листа никогда не получают писем и "
                      "пропускаются при импорте.")
        note.setWordWrap(True); note.setStyleSheet("color:#888")
        lay.addWidget(note)

        bar = QHBoxLayout()
        btn_add = QPushButton("Добавить адрес…")
        btn_add.clicked.connect(self._add_suppression)
        btn_rm = QPushButton("Убрать из стоп-листа")
        btn_rm.clicked.connect(self._remove_suppression)
        bar.addWidget(btn_add); bar.addWidget(btn_rm); bar.addStretch()
        lay.addLayout(bar)

        self.supp_table = QTableWidget(0, 3)
        self.supp_table.setHorizontalHeaderLabels(["Email", "Причина", "Дата"])
        self.supp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.supp_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.supp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.supp_table)
        return page

    # ---------------- данные ----------------

    def refresh(self):
        rows = self.storage.list_contacts()
        self.table.setRowCount(len(rows))
        for r, c in enumerate(rows):
            values = [str(c["id"]), c["email"], c["first_name"],
                      c["last_name"], c["company"], c["status"]]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col == 5 and c["status"] != "active":
                    item.setForeground(Qt.gray)
                self.table.setItem(r, col, item)
        active = self.storage.count_contacts(status="active")
        self.lbl_count.setText(f"Всего: {len(rows)}   Активных: {active}")

        supp = self.storage.list_suppression()
        self.supp_table.setRowCount(len(supp))
        for r, s in enumerate(supp):
            for col, val in enumerate([s["email"], s["reason"], s["created_at"][:19]]):
                self.supp_table.setItem(r, col, QTableWidgetItem(val))

    # ---------------- действия ----------------

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл со списком", "",
            "Списки (*.csv *.tsv *.txt *.xlsx *.xlsm);;Все файлы (*.*)")
        if not path:
            return
        try:
            headers, data_rows = contacts_mod.read_table(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Импорт", f"Не удалось прочитать файл:\n{e}")
            return
        if not data_rows:
            QMessageBox.information(self, "Импорт", "В файле нет строк с данными.")
            return

        guessed = contacts_mod.guess_mapping(headers)
        dlg = _MappingDialog(headers, guessed, self)
        if dlg.exec() != QDialog.Accepted:
            return
        mapping = dlg.mapping()
        try:
            result = contacts_mod.import_rows(
                self.storage, headers, data_rows, mapping, source=path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Импорт", str(e))
            return
        self.refresh()
        msg = result.summary()
        if result.invalid_samples:
            msg += "\n\nПримеры невалидных: " + ", ".join(result.invalid_samples[:5])
        QMessageBox.information(self, "Импорт завершён", msg)

    def _add_manual(self):
        email, ok = QInputDialog.getText(self, "Новый контакт", "Email:")
        if not ok or not email.strip():
            return
        norm = contacts_mod.normalize_email(email)
        if not norm:
            QMessageBox.warning(self, "Контакт", "Некорректный email.")
            return
        if self.storage.is_suppressed(norm):
            QMessageBox.warning(self, "Контакт", "Этот адрес в стоп-листе.")
            return
        name, _ = QInputDialog.getText(self, "Новый контакт", "Имя (необязательно):")
        self.storage.upsert_contact(norm, first_name=name.strip(), source="manual")
        self.refresh()

    def _selected_emails(self, table, col=1):
        rows = {i.row() for i in table.selectedItems()}
        return [table.item(r, col).text() for r in sorted(rows) if table.item(r, col)]

    def _selected_ids(self):
        rows = {i.row() for i in self.table.selectedItems()}
        return [int(self.table.item(r, 0).text()) for r in sorted(rows) if self.table.item(r, 0)]

    def _delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "Удаление",
                                f"Удалить {len(ids)} контакт(ов) из базы?") != QMessageBox.Yes:
            return
        for cid in ids:
            self.storage.delete_contact(cid)
        self.refresh()

    def _suppress_selected(self):
        emails = self._selected_emails(self.table)
        if not emails:
            return
        for e in emails:
            self.storage.add_suppression(e, reason="manual")
        self.refresh()

    def _add_suppression(self):
        email, ok = QInputDialog.getText(self, "Стоп-лист", "Email для блокировки:")
        if not ok or not email.strip():
            return
        norm = contacts_mod.normalize_email(email) or email.strip().lower()
        self.storage.add_suppression(norm, reason="manual")
        self.refresh()

    def _remove_suppression(self):
        emails = self._selected_emails(self.supp_table, col=0)
        for e in emails:
            self.storage.remove_suppression(e)
        self.refresh()
