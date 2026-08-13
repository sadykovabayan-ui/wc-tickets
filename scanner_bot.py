#!/usr/bin/env python3
"""
WC Fest 2026 — Telegram-бот сканера билетов (long polling).
Запускается в GitHub Actions (см. .github/workflows/scanner-bot.yml).

ENV:
  BOT_TOKEN     — токен бота от @BotFather
  AMOCRM_TOKEN  — токен AmoCRM
  ACCESS_CODE   — код доступа волонтёров (например: wc26)
  RUN_MINUTES   — сколько минут работать до самоперезапуска (default 340)

Волонтёр: /start → код доступа → кнопка «📷 Сканировать» (mini app с нативным
QR-сканером Telegram) → номер прилетает боту → проверка AmoCRM → ✅/🚫 за ~2 сек.
Ручной ввод номера текстом тоже работает.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

BOT = os.environ["BOT_TOKEN"]
AMO_TOKEN = os.environ["AMOCRM_TOKEN"]
ACCESS_CODE = os.environ.get("ACCESS_CODE", "wc26").strip().lower()
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "340"))

TG = f"https://api.telegram.org/bot{BOT}"
AMO = "https://womancreate.amocrm.ru/api/v4"
SCANNER_URL = "https://sadykovabayan-ui.github.io/wc-tickets/scanner.html"
ALMATY_TZ = timezone(timedelta(hours=5))

PIPES = {
    10952890: {"city": "Алматы", "paid": 86119638, "sent": 86119642},
    10952898: {"city": "Астана", "paid": 86119678, "sent": 86119682},
}
F_TICKET = 1173752
F_SCANTIME = 1173762
F_VOLUNTEER = 1173764
STATUS_DONE = 142

TICKET_RE = re.compile(r"WCF26-(?:ALA|AST)-\d{4}", re.I)

# Авторизованные волонтёры держим в памяти + файл-кэш между перезапусками не нужен:
# при перезапуске волонтёр просто снова введёт код (редко, раз в ~6 часов).
volunteers = {}  # chat_id -> name


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(url, payload=None, timeout=35):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode() or "{}")
        except Exception: return {}
    except Exception as e:
        log(f"http error: {e}")
        return {}


def amo(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(AMO + path, data=data, method=method,
        headers={"Authorization": f"Bearer {AMO_TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            b = r.read().decode()
            return r.status, (json.loads(b) if b else None)
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, None
    except Exception as e:
        log(f"amo error: {e}")
        return 0, None


def send(chat_id, text, with_keyboard=True):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if with_keyboard:
        p["reply_markup"] = {
            "keyboard": [
                [{"text": "📷 Сканировать билет", "web_app": {"url": SCANNER_URL}}],
                [{"text": "📊 Статистика"}],
            ],
            "resize_keyboard": True, "is_persistent": True,
        }
    http(f"{TG}/sendMessage", p)


def cf(lead, field_id):
    for c in lead.get("custom_fields_values") or []:
        if c.get("field_id") == field_id:
            vals = c.get("values") or []
            return str(vals[0].get("value") or "") if vals else ""
    return ""


def find_lead(num):
    st, resp = amo("GET", f"/leads?query={urllib.parse.quote(num)}&with=contacts,custom_fields_values&limit=10")
    if st != 200 or not resp:
        return None
    for lead in (resp.get("_embedded", {}).get("leads") or []):
        if num in cf(lead, F_TICKET).upper():
            return lead
    return None


def guest_name(lead):
    cs = (lead.get("_embedded") or {}).get("contacts") or []
    if not cs:
        return lead.get("name", "")
    st, c = amo("GET", f"/contacts/{cs[0]['id']}")
    return (c or {}).get("name") or lead.get("name", "")


def check_ticket(num, volunteer):
    num = num.upper()
    lead = find_lead(num)
    if not lead:
        return f"❌ <b>БИЛЕТ НЕ НАЙДЕН</b>\n<code>{num}</code>\nПроверь номер или зови старшего."

    pipe = PIPES.get(lead.get("pipeline_id"))
    if not pipe:
        return f"⚠️ <b>СТОП</b>\n<code>{num}</code> — сделка в служебной воронке. Зови старшего."

    if lead.get("status_id") not in (pipe["sent"], pipe["paid"], STATUS_DONE):
        return (f"⚠️ <b>СТОП — оплата не подтверждена</b>\n<code>{num}</code>\n"
                f"Статус сделки не «Билет отправлен». Зови старшего.")

    all_tickets = TICKET_RE.findall(cf(lead, F_TICKET).upper()) or [num]
    scanned = cf(lead, F_VOLUNTEER)  # "0137@15:02;0142@15:04"
    short = num[-4:]
    if f"{short}@" in scanned:
        t = scanned.split(f"{short}@")[1].split(";")[0]
        return (f"🚫 <b>УЖЕ ИСПОЛЬЗОВАН</b>\n<code>{num}</code>\n"
                f"Сканирован сегодня в <b>{t}</b>.\nНе пропускать! Зови старшего.")

    now = datetime.now(ALMATY_TZ)
    hhmm = now.strftime("%H:%M")
    new_scanned = f"{scanned};{short}@{hhmm}" if scanned else f"{short}@{hhmm}"
    scanned_count = len(new_scanned.split(";"))
    all_done = scanned_count >= len(all_tickets)

    fields = [{"field_id": F_VOLUNTEER, "values": [{"value": new_scanned}]}]
    if not scanned:
        fields.append({"field_id": F_SCANTIME, "values": [{"value": int(now.timestamp())}]})
    patch = {"custom_fields_values": fields}
    if all_done:
        patch["status_id"] = STATUS_DONE
    st, _ = amo("PATCH", f"/leads/{lead['id']}", patch)
    if st not in (200, 202):
        return f"⚠️ Сбой записи в CRM (HTTP {st}). Отсканируй ещё раз."

    amo("POST", f"/leads/{lead['id']}/notes",
        [{"note_type": "common", "params": {"text": f"🎟 Скан {num} в {hhmm}, волонтёр: {volunteer}"}}])

    name = guest_name(lead)
    extra = (f"\n👥 Заказ на {len(all_tickets)}: отсканировано {scanned_count} из {len(all_tickets)}"
             if len(all_tickets) > 1 else "")
    return f"✅ <b>ПРОХОДИТ</b>\n<b>{name}</b>\n<code>{num}</code> · {pipe['city']} · {hhmm}{extra}"


def stats():
    out = "📊 <b>Статистика входа</b>\n"
    for pid, pipe in PIPES.items():
        scanned = total = 0
        page = 1
        while page <= 6:
            st, resp = amo("GET", f"/leads?filter[pipeline_id]={pid}&with=custom_fields_values&limit=250&page={page}")
            if st != 200 or not resp:
                break
            leads = resp.get("_embedded", {}).get("leads") or []
            if not leads:
                break
            for lead in leads:
                total += len(TICKET_RE.findall(cf(lead, F_TICKET)))
                sc = cf(lead, F_VOLUNTEER)
                if sc:
                    scanned += len(sc.split(";"))
            if len(leads) < 250:
                break
            page += 1
        out += f"\n<b>{pipe['city']}</b>: прошло {scanned} из {total} билетов"
    return out


def handle(msg):
    chat_id = msg["chat"]["id"]
    frm = msg.get("from") or {}
    from_name = f"{frm.get('first_name','')} {frm.get('last_name','')}".strip() or f"id{chat_id}"

    # Данные из mini app (QR отсканирован)
    wad = (msg.get("web_app_data") or {}).get("data", "")
    if wad:
        if chat_id not in volunteers:
            send(chat_id, "🔒 Сначала введи код доступа (спроси у организатора).", with_keyboard=False)
            return
        send(chat_id, check_ticket(wad, volunteers[chat_id]))
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text == "/start":
        if chat_id in volunteers:
            send(chat_id, "С возвращением! Жми кнопку и сканируй 🎟")
        else:
            send(chat_id, "👋 Это сканер билетов фестиваля «Я боюсь и делаю!»\n\n"
                          "🔒 Введи <b>код доступа</b> (спроси у организатора):", with_keyboard=False)
        return

    if chat_id not in volunteers:
        if text.lower() == ACCESS_CODE:
            volunteers[chat_id] = from_name
            send(chat_id, f"✅ Доступ открыт, {from_name}!\nЖми кнопку — откроется сканер.")
        else:
            send(chat_id, "❌ Неверный код. Попробуй ещё раз.", with_keyboard=False)
        return

    if text in ("📊 Статистика", "/stats"):
        send(chat_id, stats())
        return

    m = TICKET_RE.search(text.upper())
    if m:
        send(chat_id, check_ticket(m.group(0), volunteers[chat_id]))
        return

    send(chat_id, "Жми «📷 Сканировать билет» или пришли номер (WCF26-ALA-0001).")


def main():
    deadline = time.time() + RUN_MINUTES * 60
    offset = 0
    log(f"Бот запущен, работаю {RUN_MINUTES} минут")
    # Снимаем вебхук на случай если стоял — long polling иначе конфликтует
    http(f"{TG}/deleteWebhook")
    while time.time() < deadline:
        upd = http(f"{TG}/getUpdates", {"offset": offset, "timeout": 25,
                                         "allowed_updates": ["message"]}, timeout=35)
        for u in upd.get("result") or []:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle(u["message"])
            except Exception as e:
                log(f"handle error: {e}")
    log("Время вышло — завершаюсь (workflow перезапустит)")


if __name__ == "__main__":
    sys.exit(main())
