"""Запуск MailSender из корня проекта.

    python run.py          # десктоп: стеклянное окно (pywebview)
    python run.py web      # веб-сервер (FastAPI) на http://localhost:8000
    python run.py qt       # классический интерфейс на Qt

В Docker запускается веб-сервер через uvicorn (см. Dockerfile).
"""

import os
import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "web":
        import uvicorn
        host = os.environ.get("MAILSENDER_HOST", "127.0.0.1")
        port = int(os.environ.get("MAILSENDER_PORT", "8000"))
        # один воркер: движок рассылки держит состояние в памяти процесса
        uvicorn.run("mailsender.webserver:app", host=host, port=port, workers=1)
        return 0
    if mode == "qt":
        from mailsender.main import main as qt_main
        return qt_main()
    from mailsender.webmain import main as web_main
    return web_main()


if __name__ == "__main__":
    raise SystemExit(main())
