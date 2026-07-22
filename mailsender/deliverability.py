"""Проверка доставляемости домена отправителя: SPF, DKIM, DMARC.

Три DNS-записи решают, попадёт письмо во «Входящие» или в спам:
  * SPF   — какие серверы вправе слать почту от имени домена;
  * DKIM  — криптоподпись писем (проверяется по селектору);
  * DMARC — политика на случай, если SPF/DKIM не сошлись.

Модуль только читает публичные TXT-записи DNS и даёт вердикт с подсказкой.
Ничего не меняет — настройку записей делает администратор домена.
"""

from __future__ import annotations

try:
    import dns.resolver
    _HAVE_DNS = True
except Exception:
    _HAVE_DNS = False

# Типовые селекторы DKIM у популярных провайдеров — пробуем их,
# т.к. точный селектор известен только из заголовков реального письма.
_DKIM_SELECTORS = ["default", "google", "selector1", "selector2", "mail",
                   "k1", "dkim", "s1", "s2", "mandrill", "sendgrid", "zoho"]


def _txt(name: str) -> list[str]:
    if not _HAVE_DNS:
        return []
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=5)
    except Exception:
        return []
    out = []
    for r in answers:
        try:
            out.append(b"".join(r.strings).decode(errors="replace"))
        except Exception:
            out.append(str(r))
    return out


def check_domain(domain: str) -> dict:
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return {"ok": False, "message": "Не указан домен"}
    if not _HAVE_DNS:
        return {"ok": False, "message": "Не установлен dnspython (pip install dnspython)"}

    # SPF
    spf_records = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    spf = _verdict(
        bool(spf_records),
        "SPF настроен" if spf_records else "SPF-запись не найдена",
        "Добавьте TXT-запись вида «v=spf1 include:_spf.вашпровайдер -all».",
        detail=spf_records[0] if spf_records else "",
    )

    # DMARC
    dmarc_records = [t for t in _txt("_dmarc." + domain) if "v=dmarc1" in t.lower()]
    dmarc = _verdict(
        bool(dmarc_records),
        "DMARC настроен" if dmarc_records else "DMARC-запись не найдена",
        "Добавьте TXT-запись _dmarc с «v=DMARC1; p=none; rua=mailto:…» "
        "и позже ужесточите политику до quarantine/reject.",
        detail=dmarc_records[0] if dmarc_records else "",
    )

    # DKIM (перебор типовых селекторов)
    found_selectors = []
    for sel in _DKIM_SELECTORS:
        recs = _txt(f"{sel}._domainkey.{domain}")
        if any("v=dkim1" in r.lower() or "p=" in r.lower() for r in recs):
            found_selectors.append(sel)
    dkim = _verdict(
        bool(found_selectors),
        f"DKIM найден (селектор: {', '.join(found_selectors)})" if found_selectors
        else "DKIM по типовым селекторам не найден",
        "DKIM настраивается в панели почтового провайдера; селектор может быть "
        "нестандартным — тогда проверьте подпись отправкой письма на тест-сервис.",
        detail="",
        soft=not found_selectors,  # отсутствие может быть ложным (нестандартный селектор)
    )

    checks = [spf, dkim, dmarc]
    score = sum(1 for c in checks if c["ok"])
    return {
        "ok": True,
        "domain": domain,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "score": score,
        "summary": f"Настроено {score} из 3 ключевых записей.",
    }


def _verdict(ok: bool, title: str, hint: str, *, detail: str = "", soft: bool = False) -> dict:
    return {"ok": ok, "title": title, "hint": "" if ok else hint,
            "detail": detail, "soft": soft}
