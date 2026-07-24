"""Веб-сервер (FastAPI): тот же Api, но по HTTP — для запуска на VPS в Docker.

Архитектура намеренно простая (один общий инстанс для отдела продаж):
  * методы Api доступны как POST /api/<method> с телом-массивом аргументов;
  * прогресс рассылки уходит в браузер через SSE (/events);
  * импорт файла — загрузкой (/api/upload_import), т.к. нативного диалога нет;
  * вход по одному общему паролю (MAILSENDER_ACCESS_PASSWORD) — это ключ от
    общего окна, а не регистрация. Без него панель на публичном IP была бы
    открытым спам-релеем.

Запуск:  python run.py web   (или uvicorn mailsender.webserver:app)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .api import Api

_HERE = Path(__file__).resolve().parent
_WEBAPP = _HERE / "webapp"

ACCESS_PASSWORD = os.environ.get("MAILSENDER_ACCESS_PASSWORD", "")
AUTH_ENABLED = bool(ACCESS_PASSWORD)
COOKIE = "ms_session"
SESSION_TTL = 7 * 24 * 3600

# Методы Api, которые НЕ отдаём наружу (служебные/desktop-специфичные).
_HIDDEN = {"pick_import_file", "shutdown", "start_scheduler"}


# ---------------- SSE-брокер (мост поток→event loop) ----------------

class Broadcaster:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop):
        self.loop = loop

    def publish(self, event: str, payload) -> None:
        """Вызывается из фонового потока рассылки — раскидываем подписчикам."""
        if self.loop is None:
            return
        item = {"event": event, "data": payload}
        for q in list(self.subs):
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:
                pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self.subs.discard(q)


broadcaster = Broadcaster()
api = Api()
api.emit = broadcaster.publish

# Активные сессии: token -> expiry_ts
_sessions: dict[str, float] = {}


def _valid_session(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


app = FastAPI(title="MailSender")


@app.on_event("startup")
async def _startup():
    broadcaster.bind_loop(asyncio.get_running_loop())
    # фоновый планировщик цепочек писем (отправка шагов по задержкам)
    api.start_scheduler()
    if not AUTH_ENABLED:
        print("[MailSender] ВНИМАНИЕ: MAILSENDER_ACCESS_PASSWORD не задан — "
              "вход без пароля. Для VPS обязательно задайте пароль.")


@app.on_event("shutdown")
async def _shutdown():
    api.shutdown()


# ---------------- аутентификация ----------------

@app.get("/auth/status")
async def auth_status(request: Request):
    return {"auth_enabled": AUTH_ENABLED, "authenticated": _valid_session(request)}


@app.post("/auth/login")
async def auth_login(request: Request, response: Response, body: dict = Body(...)):
    if not AUTH_ENABLED:
        return {"ok": True}
    password = (body or {}).get("password", "")
    if not secrets.compare_digest(str(password), ACCESS_PASSWORD):
        return JSONResponse({"ok": False, "message": "Неверный пароль"}, status_code=401)
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        samesite="lax")
    return {"ok": True}


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------- события (SSE) ----------------

@app.get("/events")
async def events(request: Request):
    if not _valid_session(request):
        return JSONResponse({"message": "unauthorized"}, status_code=401)
    q = await broadcaster.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield (f"event: {item['event']}\n"
                       f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n")
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------- вызов методов Api ----------------
# ВНИМАНИЕ: специфичные /api/* маршруты объявляем ДО общего /api/{method},
# иначе он перехватит их (Starlette матчит маршруты по порядку).

@app.post("/api/upload_import")
async def upload_import(request: Request, file: UploadFile = File(...)):
    if not _valid_session(request):
        return JSONResponse({"message": "unauthorized"}, status_code=401)
    suffix = Path(file.filename or "list.csv").suffix or ".csv"
    tmp_dir = Path(tempfile.gettempdir()) / "mailsender_imports"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{secrets.token_hex(8)}{suffix}"
    data = await file.read()
    tmp_path.write_bytes(data)
    result = await run_in_threadpool(api.preview_import, str(tmp_path))
    return JSONResponse(result)


@app.post("/api/{method}")
async def call_api(method: str, request: Request, args: list = Body(default=[])):
    if not _valid_session(request):
        return JSONResponse({"message": "unauthorized"}, status_code=401)
    if method in _HIDDEN or method.startswith("_"):
        return JSONResponse({"message": "not found"}, status_code=404)
    fn = getattr(api, method, None)
    if not callable(fn):
        return JSONResponse({"message": "not found"}, status_code=404)
    if not isinstance(args, list):
        args = [args]
    try:
        result = await run_in_threadpool(fn, *args)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)
    return JSONResponse(result)


@app.get("/health")
async def health():
    return {"status": "ok"}


# статика (index.html, style.css, app.js, background.svg) — в самом конце,
# чтобы не перехватывать /api и /events.
app.mount("/", StaticFiles(directory=str(_WEBAPP), html=True), name="static")
