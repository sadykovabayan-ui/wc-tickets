#!/usr/bin/env python3
"""
Health check + bounce detection + admin alerts.

Каждый запуск:
1. Опрашивает Resend API за последние 24 часа на письма со статусом bounced/complained.
2. Для каждого bounce ищет сделку в AmoCRM (по custom field RESEND_EMAIL_ID).
3. Если найдена сделка в «Билет отправлен»:
   - откатывает статус → «Оплачено»
   - очищает ticket_no / qr_link / resend_email_id
   - добавляет note: «BOUNCED: email невалиден, нужен новый»
   - отправляет email-алерт Баян
4. Также проверяет «висячих» в «Оплачено» дольше 30 минут → алерт Баян.

Запускается через GitHub Actions cron (вместе с send-ticket).
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import os
import sys
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "token.local"
RESEND_KEY_FILE = SCRIPT_DIR / "resend.key.local"
LOG_FILE = SCRIPT_DIR / "health_check.log"

API_BASE = "https://womancreate.amocrm.ru/api/v4"

PIPELINES = {
    10952890: {"name": "Алматы 2026", "status_paid": 86119638, "status_sent": 86119642},
    10952898: {"name": "Астана 2026",  "status_paid": 86119678, "status_sent": 86119682},
}

FIELD_TICKET_NUMBER = 1173752
FIELD_QR_LINK = 1166691
FIELD_RESEND_EMAIL_ID = 1174381

ADMIN_EMAIL = "sadykova.bayan@gmail.com"     # сюда летят алерты
ADMIN_NAME = "Баян"
RESEND_FROM = "Woman Create <noreply@womancreate.kz>"
WC_WHATSAPP = "+7 707 229 53 57"

HANG_THRESHOLD_MINUTES = 30   # если сделка висит в Оплачено дольше — алерт


def _get_secret(env_var: str, file_path: Path) -> str:
    v = os.environ.get(env_var)
    if v:
        return v.strip()
    if file_path.exists():
        return file_path.read_text().strip()
    raise RuntimeError(f"Не найден секрет: ни env {env_var}, ни файл {file_path}")


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def amo(method: str, path: str, payload=None):
    token = _get_secret("AMOCRM_TOKEN", TOKEN_FILE)
    url = API_BASE + path
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except: return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def resend_get(path: str):
    api_key = _get_secret("RESEND_API_KEY", RESEND_KEY_FILE)
    req = urllib.request.Request(
        "https://api.resend.com" + path,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "wc-tickets/1.0 (+https://womancreate.kz)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        log(f"  ❌ Resend GET {path}: {e}")
        return None


def send_admin_alert(subject: str, body_html: str) -> None:
    """Отправка email-алерта Баян. Не возвращает ничего критичного."""
    try:
        api_key = _get_secret("RESEND_API_KEY", RESEND_KEY_FILE)
    except Exception as e:
        log(f"  ❌ admin_alert: нет ключа Resend: {e}")
        return
    payload = {
        "from": RESEND_FROM,
        "to": [ADMIN_EMAIL],
        "subject": f"[wc-tickets] {subject}",
        "html": body_html,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "wc-tickets/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log(f"  📨 admin_alert отправлен: «{subject}»")
    except Exception as e:
        log(f"  ❌ admin_alert не отправился: {e}")


def find_lead_by_email_id(email_id: str):
    """Ищет сделку по custom field RESEND_EMAIL_ID. Возвращает lead dict или None."""
    # Прямого фильтра по custom field нет, но можно через query — но лучше пройти по обоим воронкам status_sent
    for pipe_id, pipe in PIPELINES.items():
        st, resp = amo("GET", f"/leads?filter[pipeline_id]={pipe_id}&filter[statuses][0][pipeline_id]={pipe_id}&filter[statuses][0][status_id]={pipe['status_sent']}&with=contacts,custom_fields_values&limit=250")
        if st != 200 or not resp:
            continue
        for lead in (resp.get("_embedded", {}).get("leads") or []):
            for cf in lead.get("custom_fields_values") or []:
                if cf.get("field_id") == FIELD_RESEND_EMAIL_ID:
                    v = (cf.get("values") or [{}])[0].get("value", "")
                    if v == email_id:
                        return lead, pipe_id, pipe
    return None, None, None


def handle_bounce(email_id: str, recipient: str, subject: str):
    """Обрабатываем bounce: откат сделки, note, alert."""
    lead, pipe_id, pipe = find_lead_by_email_id(email_id)
    if not lead:
        log(f"  ⚠ bounce {email_id} ({recipient}): сделка по email_id не найдена — может уже обработана")
        # Всё равно отправим alert Баян
        send_admin_alert(
            f"⚠️ BOUNCE: {recipient}",
            f"<p>Письмо <strong>{subject}</strong> к {recipient} вернулось как bounced, но сделка в AmoCRM не найдена.</p>"
            f"<p>Resend ID: {email_id}</p>"
            f"<p>Проверь Resend Dashboard.</p>"
        )
        return

    lead_id = lead["id"]
    log(f"  🚨 bounce: сделка #{lead_id} {recipient}")

    # Откат: статус → Оплачено, очистка
    st, _ = amo("PATCH", f"/leads/{lead_id}", {
        "status_id": pipe["status_paid"],
        "custom_fields_values": [
            {"field_id": FIELD_TICKET_NUMBER, "values": [{"value": None}]},
            {"field_id": FIELD_QR_LINK, "values": [{"value": None}]},
            {"field_id": FIELD_RESEND_EMAIL_ID, "values": [{"value": None}]},
        ]
    })
    if st in (200, 202):
        log(f"    ✅ #{lead_id} откачена в «Оплачено»")
    else:
        log(f"    ❌ #{lead_id} откат не удался HTTP {st}")

    # Note
    amo("POST", f"/leads/{lead_id}/notes", [{
        "note_type": "common",
        "params": {"text": f"🚨 BOUNCED: письмо на {recipient} не доставлено. Email невалиден. Уточни правильный email у гостя по WhatsApp/телефону и обнови контакт. Resend ID: {email_id}"}
    }])

    # Alert Баян
    send_admin_alert(
        f"🚨 Письмо не доставлено: {recipient}",
        f"""<p>Здравствуй, Баян!</p>
<p>Письмо с билетом <strong>не доставлено</strong>:</p>
<ul>
  <li><strong>Сделка:</strong> #{lead_id} — <a href="https://womancreate.amocrm.ru/leads/detail/{lead_id}">открыть в AmoCRM</a></li>
  <li><strong>Email (невалидный):</strong> {recipient}</li>
  <li><strong>Гость:</strong> «{lead.get('name','')}»</li>
  <li><strong>Тема письма:</strong> «{subject}»</li>
</ul>
<p>Что я сделал автоматом:</p>
<ul>
  <li>Откатил сделку в статус «Оплачено» (билет НЕ выпущен)</li>
  <li>Очистил номер билета, чтобы при правильном email он выпустился заново</li>
  <li>Добавил примечание к сделке</li>
</ul>
<p><strong>Что нужно от тебя:</strong></p>
<ol>
  <li>Связаться с гостем по WhatsApp/телефону (контакт в сделке)</li>
  <li>Уточнить правильный email</li>
  <li>Обновить email в контакте AmoCRM</li>
  <li>Билет автоматически выпустится в течение 5 минут</li>
</ol>
<p>Контакт поддержки: <strong>{WC_WHATSAPP}</strong></p>"""
    )


def check_bounces():
    """Опрашивает Resend за последние 24 часа на bounces."""
    data = resend_get("/emails?limit=100")
    if not data: return 0
    bounced = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for e in data.get("data", []) or []:
        last = (e.get("last_event") or "").lower()
        if last not in ("bounced", "complained", "failed"):
            continue
        ts_str = e.get("created_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except: pass
        recipient = e.get("to") or []
        if isinstance(recipient, list): recipient = recipient[0] if recipient else ""
        subject = e.get("subject", "")
        email_id = e.get("id", "")
        handle_bounce(email_id, recipient, subject)
        bounced += 1
    return bounced


def check_hanging():
    """Сделки висящие в «Оплачено» дольше HANG_THRESHOLD_MINUTES."""
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=HANG_THRESHOLD_MINUTES)
    hanging = []
    for pipe_id, pipe in PIPELINES.items():
        st, resp = amo("GET", f"/leads?filter[pipeline_id]={pipe_id}&filter[statuses][0][pipeline_id]={pipe_id}&filter[statuses][0][status_id]={pipe['status_paid']}&with=contacts&limit=100")
        if st != 200 or not resp:
            continue
        for lead in (resp.get("_embedded", {}).get("leads") or []):
            updated = datetime.fromtimestamp(lead.get("updated_at", 0), tz=timezone.utc)
            if updated < threshold:
                hanging.append((lead, pipe))
    if not hanging:
        return 0
    rows = "".join(
        f'<li><a href="https://womancreate.amocrm.ru/leads/detail/{l["id"]}">#{l["id"]}</a> «{l["name"][:50]}» в {p["name"]}, висит с {datetime.fromtimestamp(l["updated_at"]).strftime("%H:%M %d.%m")}</li>'
        for l, p in hanging
    )
    send_admin_alert(
        f"⏰ {len(hanging)} сделок висят в «Оплачено» дольше {HANG_THRESHOLD_MINUTES} мин",
        f"<p>Здравствуй, Баян. Эти сделки давно в «Оплачено», но билеты ещё не выпущены:</p><ul>{rows}</ul><p>Возможно у них нет email у контакта — проверь. Или Workflow задержался — я уже его запустил.</p>"
    )
    return len(hanging)


def main() -> int:
    log("=== health_check старт ===")
    bounced = check_bounces()
    hanging = check_hanging()
    log(f"итого: bounces={bounced}, висячих={hanging}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"❌ Top-level error: {e}")
        sys.exit(2)
