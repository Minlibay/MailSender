"""Точка входа веб-версии интерфейса (glassmorphism).

Рендерит HTML/CSS во встроенном окне через pywebview (на Windows — движок
WebView2/Edge, поэтому работает настоящий backdrop-filter blur).
Весь Python-бэкенд (storage/smtp/imap/campaign) переиспользуется через Api.

Запуск:  python run.py   или   python -m mailsender.webmain
"""

from __future__ import annotations

import os
import sys

import webview

from .api import Api

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX = os.path.join(_HERE, "webapp", "index.html")


def main() -> int:
    api = Api()
    window = webview.create_window(
        title="MailSender",
        url=_INDEX,
        js_api=api,
        width=1240,
        height=840,
        min_size=(980, 640),
        background_color="#20242a",
    )
    api.window = window
    # фоновый планировщик цепочек писем
    api.start_scheduler()

    try:
        # gui=None — pywebview сам выберет доступный движок (WebView2 на Windows).
        webview.start()
    finally:
        api.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
