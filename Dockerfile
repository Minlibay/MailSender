# MailSender — веб-сервер в контейнере.
FROM python:3.12-slim

# Небуферизованный вывод логов + без .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAILSENDER_DATA_DIR=/data \
    MAILSENDER_HOST=0.0.0.0 \
    MAILSENDER_PORT=8000

WORKDIR /app

# Сначала зависимости — лучше кешируется при пересборке
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# Код приложения (бэкенд + статика веб-интерфейса)
COPY mailsender ./mailsender

# Том для БД, настроек и пароля SMTP
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Один воркер: движок рассылки хранит состояние в памяти процесса
CMD ["uvicorn", "mailsender.webserver:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
