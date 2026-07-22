# Развёртывание MailSender на Ubuntu 24.04 (VPS)

Пошагово: от чистого сервера до рабочей панели по HTTPS. Всё крутится в Docker,
данные — в томе, доступ закрыт общим паролем.

Ориентировочное время — 15–20 минут.

---

## 0. Что нужно заранее

- VPS с **Ubuntu 24.04**, доступ по SSH под пользователем с `sudo`.
- (Рекомендуется) доменное имя, указывающее на IP сервера — для HTTPS.
  Например `mail.вашдомен.ru → A-запись → IP сервера`.
- Доступы к корпоративному **SMTP** (хост, порт, логин, пароль).

Дальше все команды выполняются на сервере под обычным пользователем с `sudo`.

---

## 1. Обновить систему и поставить Docker

```bash
sudo apt update && sudo apt upgrade -y

# Docker Engine + Compose plugin (официальный способ)
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# запускать docker без sudo (перелогиньтесь после этой команды)
sudo usermod -aG docker $USER
newgrp docker

docker --version && docker compose version
```

---

## 2. Забрать код и настроить секреты

```bash
cd ~
git clone https://github.com/Minlibay/MailSender.git
cd MailSender

# создать .env из шаблона и задать пароль доступа
cp .env.example .env
nano .env
```

В `.env` задайте общий пароль на вход (минимум — измените `MAILSENDER_ACCESS_PASSWORD`):

```ini
MAILSENDER_ACCESS_PASSWORD=придумайте-длинный-пароль
# необязательно: пароль SMTP можно задать здесь, тогда не придётся вводить в UI
MAILSENDER_SMTP_PASSWORD=
```

> `.env` в репозиторий не попадает (он в `.gitignore`) — секреты остаются только на сервере.

---

## 3. Запустить

```bash
docker compose up -d --build
```

Проверить, что живо:

```bash
docker compose ps
curl -s http://127.0.0.1:8000/health   # должно вернуть {"status":"ok"}
docker compose logs -f                 # логи (Ctrl+C для выхода)
```

Данные (БД, настройки, пароль SMTP) лежат в каталоге `./data` рядом с проектом —
он монтируется в контейнер и переживает пересборки.

---

## 4. Открыть доступ безопасно

Приложение слушает `8000` **внутри контейнера**. Наружу его лучше не выставлять
напрямую, а пустить через nginx с HTTPS. Настройте фаервол:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

> Порт `8000` в `ufw` НЕ открываем — доступ к панели только через nginx (шаг 5).
> `docker-compose.yml` пробрасывает порт на `127.0.0.1`, поэтому снаружи он не виден.

Сначала ограничим публикацию порта только локалхостом — откройте `docker-compose.yml`
и убедитесь, что строка портов такая:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

(если стоит `"8000:8000"` — поправьте на `"127.0.0.1:8000:8000"` и `docker compose up -d`).

---

## 5. HTTPS через nginx + Let's Encrypt

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

Создать конфиг сайта (замените `mail.вашдомен.ru` на свой домен):

```bash
sudo nano /etc/nginx/sites-available/mailsender
```

```nginx
server {
    listen 80;
    server_name mail.вашдомен.ru;

    client_max_body_size 25m;   # для загрузки файлов со списками

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # для SSE (живой прогресс рассылки) — не буферизуем поток
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/mailsender /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# выпустить сертификат и включить HTTPS автоматически
sudo certbot --nginx -d mail.вашдомен.ru
```

Certbot сам добавит редирект на HTTPS и настроит автопродление.
Теперь панель доступна по `https://mail.вашдомен.ru`, вход — по паролю из `.env`.

> Без домена для теста можно временно открыть панель по `http://IP-сервера:8000`:
> в `docker-compose.yml` поменяйте порт на `"0.0.0.0:8000:8000"`, выполните
> `docker compose up -d` и `sudo ufw allow 8000/tcp`. Тогда пароль и данные идут
> по сети без шифрования — годится только для проверки, не для работы.

---

## 6. Настроить доставляемость (чтобы письма шли во «Входящие»)

В DNS домена, с которого шлёте, добавьте записи (у вашего DNS-провайдера):

- **SPF** — TXT на домене: `v=spf1 include:_spf.вашсмтп -all`
- **DKIM** — включается в панели почтового провайдера (даст TXT-запись с селектором)
- **DMARC** — TXT на `_dmarc.домен`: `v=DMARC1; p=none; rua=mailto:postmaster@домен`

Проверить можно прямо в приложении: **Настройки → Доставляемость домена → Проверить домен**.

---

## Обслуживание

**Обновить до новой версии:**
```bash
cd ~/MailSender
git pull
docker compose up -d --build
```

**Логи / перезапуск / остановка:**
```bash
docker compose logs -f
docker compose restart
docker compose down          # остановить (данные в ./data сохранятся)
```

**Бэкап данных** (БД, настройки, пароль SMTP):
```bash
tar czf mailsender-backup-$(date +%F).tar.gz -C ~/MailSender data
```

**Сменить пароль доступа:** отредактируйте `MAILSENDER_ACCESS_PASSWORD` в `.env` и
выполните `docker compose up -d` (перезапустит контейнер с новым паролем).

---

## Быстрый чек-лист

1. Docker установлен → `docker compose version` работает
2. `.env` создан, пароль задан
3. `docker compose up -d --build` → `curl /health` = ok
4. `ufw`: открыты 22/80/443, порт 8000 только на localhost
5. nginx + certbot → HTTPS работает
6. SPF/DKIM/DMARC в DNS → проверка в UI зелёная
7. Зашли по `https://домен`, ввели пароль, настроили SMTP → тестовое письмо себе дошло
