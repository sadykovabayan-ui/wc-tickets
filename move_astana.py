#!/usr/bin/env python3
"""
Dispatch — раскидывает сделки в правильные воронки и статусы.

Часть 1 — из общей воронки «Фестиваль» (7106050) в воронки 2026 (Алматы/Астана):
  «Фестиваль / Новая заявка»     →
        канал=Robokassa  → «{Город} 2026 / Ожидание оплаты Robokassa»
        канал=Kaspi      → «{Город} 2026 / Новая заявка»
        (канал пустой)   → «{Город} 2026 / Новая заявка»
  «Фестиваль / Счёт оплачен»     → «{Город} 2026 / Оплачено» (Robokassa успех)
  «Фестиваль / Robokassa нет опл.» → «{Город} 2026 / Robokassa нет оплаты»

Часть 2 — таймаут «Ожидание оплаты Robokassa»:
  Сделки старше 15 минут в «Ожидание оплаты Robokassa» (любая 2026-воронка)
  → перевод в «Robokassa нет оплаты»

Запускается через launchd раз в 5 минут.
"""

import json
import urllib.request
import urllib.error
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "token.local"
LOG_FILE = SCRIPT_DIR / "move_astana.log"


def _get_token() -> str:
    v = os.environ.get("AMOCRM_TOKEN")
    if v:
        return v.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise RuntimeError("Не найден AmoCRM token: ни env AMOCRM_TOKEN, ни файл token.local")

API_BASE = "https://womancreate.amocrm.ru/api/v4"

PIPELINE_FESTIVAL = 7106050           # Общая «Фестиваль» — для ручных переносов
PIPELINE_OSNOVNAYA = 5502967          # «Основная воронка» — куда падают заявки от Tilda по умолчанию

PIPELINES_2026 = {
    "alma": {
        "id": 10952890, "name": "Алматы 2026",
        "new_lead":         86119626,
        "wait_robokassa":   86119630,
        "kaspi_check":      86119634,
        "paid":             86119638,
        "ticket_sent":      86119642,
        "robokassa_failed": 86135206,  # новый статус
    },
    "asta": {
        "id": 10952898, "name": "Астана 2026",
        "new_lead":         86119666,
        "wait_robokassa":   86119670,
        "kaspi_check":      86119674,
        "paid":             86119678,
        "ticket_sent":      86119682,
        "robokassa_failed": 86135210,  # новый статус
    },
}

FIELD_CITY_PRIMARY = 1173497      # ВЫБОР_ГОРОДА (создано для 2026)
FIELD_CITY_LEGACY = 1160151       # Город (старое поле 2025, fallback)
FIELD_CITY_TILDA = None           # Tilda мапит «Выбор города» — id определим из данных
FIELD_PAYMENT_CHANNEL = 1173754   # Канал оплаты 2026 (legacy fallback)
FIELD_FORMID = 1101775            # FORMID (Tilda пишет автоматически)
FIELD_PAYMENTID = 1166199         # PAYMENTID (Robokassa пишет автоматически после оплаты)

# Маппинг FORMID → канал
# Считано с https://womancreate.kz/fest 27.05.2026
FORM_CHANNEL_MAP = {
    "form2237743873": "robokassa",  # Cart-форма (чекаут с корзиной, поле «Выбор города»)
    "form2237683773": "kaspi",       # Простая форма заявки #1
    "form2237748023": "kaspi",       # Простая форма заявки #2
    "form2237770963": "partner",     # Партнёрская заявка (не билет)
}

# Маппинг исходных статусов в «Фестиваль» → ключи целевых статусов в воронках 2026
SOURCE_FROM_FESTIVAL = {
    59478106: "_dispatch_by_channel",   # Новая заявка — раскидываем по каналу оплаты
    59478110: "paid",                   # Счёт оплачен → Оплачено
    86135014: "robokassa_failed",       # Robokassa нет оплаты → robokassa_failed
}

# «Основная воронка» — куда AmoCRM ставит все заявки от Tilda по умолчанию.
# Раскидываем оттуда в города 2026 на основе FORMID + PAYMENTID.
# Tilda кидает в разные статусы в зависимости от формы — поэтому ловим все «начальные» статусы.
SOURCE_FROM_OSNOVNAYA = {
    48687994: "_dispatch_by_channel",   # Неразобранное
    48687997: "_dispatch_by_channel",   # Горячий клиент (сюда падают Kaspi-формы)
    48688000: "_dispatch_by_channel",   # Биржа лидов
    62223354: "_dispatch_by_channel",   # Взят в работу (сюда падает Cart/Robokassa)
}

TIMEOUT_ROBOKASSA_SECONDS = 15 * 60   # 15 минут на оплату Robokassa
MAX_LEAD_AGE_SECONDS = 60 * 60        # обрабатываем только лиды моложе 1 часа (только свежие оплаты)


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api(method: str, path: str, payload=None):
    token = _get_token()
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
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def get_cf(lead: dict, field_id: int) -> str:
    for cf in (lead.get("custom_fields_values") or []):
        if cf.get("field_id") == field_id:
            vals = cf.get("values", [])
            if vals:
                return (vals[0].get("value") or "").strip()
    return ""


def get_cf_by_name_contains(lead: dict, substr: str) -> str:
    """Ищет кастомное поле по подстроке в названии (для нестабильных id типа «Выбор города»)."""
    substr_l = substr.lower()
    for cf in (lead.get("custom_fields_values") or []):
        fname = (cf.get("field_name") or "").lower()
        if substr_l in fname:
            vals = cf.get("values", [])
            if vals:
                return (vals[0].get("value") or "").strip()
    return ""


def resolve_target_city(lead: dict):
    """Возвращает (pipeline_cfg, city_text) либо (None, city_text) если непонятно.
    Смотрит 4 источника: FIELD_CITY_PRIMARY, FIELD_CITY_LEGACY, поле «Выбор города» по имени, тэги в названии сделки.
    """
    candidates = [
        get_cf(lead, FIELD_CITY_PRIMARY),
        get_cf(lead, FIELD_CITY_LEGACY),
        get_cf_by_name_contains(lead, "город"),
        get_cf_by_name_contains(lead, "city"),
        lead.get("name", ""),
    ]
    city = " ".join(c for c in candidates if c).lower()
    if "астана" in city or "astana" in city:
        return PIPELINES_2026["asta"], city
    if "алматы" in city or "almaty" in city:
        return PIPELINES_2026["alma"], city
    return None, city


def detect_channel(lead: dict) -> str:
    """Возвращает 'robokassa' | 'kaspi' | 'partner' | 'unknown'.
    Логика:
      1. PAYMENTID начинается с robokassa: → robokassa (оплата прошла)
      2. FORMID найден в FORM_CHANNEL_MAP → канал из маппинга
      3. Legacy FIELD_PAYMENT_CHANNEL (vibor_oplaty) если случайно заполнен
      4. fallback: kaspi (безопаснее — нет автотаймаута)
    """
    payment_id = get_cf(lead, FIELD_PAYMENTID).lower()
    if payment_id.startswith("robokassa:"):
        return "robokassa"
    if payment_id.startswith("kaspi:"):
        return "kaspi"

    form_id = get_cf(lead, FIELD_FORMID)
    if form_id in FORM_CHANNEL_MAP:
        return FORM_CHANNEL_MAP[form_id]

    legacy = get_cf(lead, FIELD_PAYMENT_CHANNEL).lower()
    if legacy == "robokassa":
        return "robokassa"
    if legacy == "kaspi":
        return "kaspi"

    return "kaspi"  # безопасный fallback — без таймаута, ждём ручной обработки


def _dispatch_pipeline(source_pipeline_id: int, source_map: dict, source_label: str):
    """Универсальная dispatch-функция. Раскидывает сделки из source_pipeline по статусам source_map в воронки 2026."""
    moved = 0
    skipped = 0
    for source_status_id, target_key in source_map.items():
        st, resp = api(
            "GET",
            f"/leads?filter[pipeline_id]={source_pipeline_id}"
            f"&filter[statuses][0][pipeline_id]={source_pipeline_id}"
            f"&filter[statuses][0][status_id]={source_status_id}"
            f"&with=custom_fields_values&limit=100",
        )
        if st == 204:
            continue
        if st != 200 or not resp:
            log(f"❌ GET {source_label} status {source_status_id}: HTTP {st}")
            continue
        leads = resp.get("_embedded", {}).get("leads", [])
        if not leads:
            continue

        now_ts = int(datetime.now(timezone.utc).timestamp())
        is_osnovnaya = (source_pipeline_id == PIPELINE_OSNOVNAYA)
        for lead in leads:
            # Защита от обработки старых лидов
            created_at = lead.get("created_at", 0)
            if created_at and (now_ts - created_at) > MAX_LEAD_AGE_SECONDS:
                continue  # тихо пропускаем — старый лид, не трогаем

            channel = detect_channel(lead)
            form_id = get_cf(lead, FIELD_FORMID)
            payment_id = get_cf(lead, FIELD_PAYMENTID).lower()

            # Для «Основной воронки»: трогаем ТОЛЬКО заявки от форм фестиваля.
            # Лиды от /form, /collab и других страниц не трогаем — пусть менеджеры обрабатывают сами.
            if is_osnovnaya and form_id not in FORM_CHANNEL_MAP:
                continue

            # Партнёрская заявка → не трогаем
            if channel == "partner":
                log(f"  ⏸ #{lead['id']}: партнёрская заявка (FORMID={form_id}), оставляю в {source_label}")
                skipped += 1
                continue

            target_pipe, city_text = resolve_target_city(lead)
            if not target_pipe:
                log(f"  ⏸ #{lead['id']}: город не определён ('{city_text[:80]}'), оставляю в {source_label}")
                skipped += 1
                continue

            # Определяем целевой статус
            if target_key == "_dispatch_by_channel":
                # Robokassa с PAYMENTID → оплата УЖЕ прошла → сразу в Оплачено
                if payment_id.startswith("robokassa:"):
                    target_status_id = target_pipe["paid"]
                    status_label = "Оплачено (Robokassa подтвердил)"
                elif channel == "robokassa":
                    # Robokassa без PAYMENTID → ждём оплату (таймаут 15 мин)
                    target_status_id = target_pipe["wait_robokassa"]
                    status_label = "Ожидание оплаты Robokassa"
                else:
                    # Kaspi или unknown → Новая заявка
                    target_status_id = target_pipe["new_lead"]
                    status_label = f"Новая заявка (канал={channel})"
            else:
                target_status_id = target_pipe[target_key]
                status_label = {
                    "paid": "Оплачено",
                    "robokassa_failed": "Robokassa нет оплаты",
                }.get(target_key, target_key)

            st2, r = api(
                "PATCH",
                f"/leads/{lead['id']}",
                {"pipeline_id": target_pipe["id"], "status_id": target_status_id},
            )
            if st2 in (200, 202):
                moved += 1
                log(f"  ✅ #{lead['id']} «{lead['name']}» {source_label} → {target_pipe['name']} / {status_label}")
            else:
                log(f"  ❌ #{lead['id']} PATCH failed: HTTP {st2}: {str(r)[:200]}")

    return moved, skipped


def dispatch_from_festival():
    """Раскидываем из «Фестиваль» + «Основная воронка» в воронки 2026."""
    m1, s1 = _dispatch_pipeline(PIPELINE_FESTIVAL, SOURCE_FROM_FESTIVAL, "Фестиваль")
    m2, s2 = _dispatch_pipeline(PIPELINE_OSNOVNAYA, SOURCE_FROM_OSNOVNAYA, "Основная")
    return m1 + m2, s1 + s2


def timeout_robokassa():
    """Часть 2: таймаут «Ожидание оплаты Robokassa» — переводит в «Robokassa нет оплаты»."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    timed_out = 0
    for key, cfg in PIPELINES_2026.items():
        st, resp = api(
            "GET",
            f"/leads?filter[pipeline_id]={cfg['id']}"
            f"&filter[statuses][0][pipeline_id]={cfg['id']}"
            f"&filter[statuses][0][status_id]={cfg['wait_robokassa']}&limit=100",
        )
        if st == 204:
            continue
        if st != 200 or not resp:
            continue
        leads = resp.get("_embedded", {}).get("leads", [])
        for lead in leads:
            # updated_at — последнее изменение, более точный показатель «висения» чем created_at
            last_change = lead.get("updated_at") or lead.get("created_at") or now_ts
            age = now_ts - last_change
            if age < TIMEOUT_ROBOKASSA_SECONDS:
                continue
            st2, r = api(
                "PATCH",
                f"/leads/{lead['id']}",
                {"status_id": cfg["robokassa_failed"]},
            )
            if st2 in (200, 202):
                timed_out += 1
                log(f"  ⏰ #{lead['id']} ({cfg['name']}): {age}s → Robokassa нет оплаты")
            else:
                log(f"  ❌ #{lead['id']} timeout PATCH failed: HTTP {st2}")
    return timed_out


def main() -> int:
    moved, skipped = dispatch_from_festival()
    timed_out = timeout_robokassa()
    if moved or skipped or timed_out:
        log(f"итого: перенесено={moved}, пропущено={skipped}, таймаут Robokassa={timed_out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"❌ Top-level error: {e}")
        sys.exit(2)
