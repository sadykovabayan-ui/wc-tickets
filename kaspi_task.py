#!/usr/bin/env python3
"""
Создание задачи "Проверь чек Kaspi" на сделках в статусе «Чек на проверке (Kaspi)».

Раз в 2 минуты:
1. Берёт все сделки в воронках Алматы 2026 / Астана 2026 со статусом «Чек на проверке (Kaspi)»
2. Для каждой — проверяет, есть ли уже открытая задача с маркером в тексте
3. Если нет — создаёт задачу на ответственного по сделке с дедлайном сегодня в конце дня
"""

import json
import urllib.request
import urllib.error
import os
import sys
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "token.local"
LOG_FILE = SCRIPT_DIR / "kaspi_task.log"


def _get_token() -> str:
    v = os.environ.get("AMOCRM_TOKEN")
    if v:
        return v.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise RuntimeError("Не найден AmoCRM token")

API_BASE = "https://womancreate.amocrm.ru/api/v4"

PIPELINES = {
    10952890: {"name": "Алматы 2026", "status_kaspi_check": 86119634},
    10952898: {"name": "Астана 2026",  "status_kaspi_check": 86119674},
}

TASK_TEXT = "💸 Проверь Kaspi-чек по этой сделке"
TASK_TYPE_ID = 1  # 1 = «Связаться» (стандартный тип задачи в AmoCRM)


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


def end_of_today_ts() -> int:
    """Возвращает timestamp 22:00 сегодня (по Астане, UTC+5)."""
    tz = timezone(timedelta(hours=5))
    now = datetime.now(tz)
    end = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if end <= now:
        end = end + timedelta(days=1)
    return int(end.timestamp())


def has_open_task(lead_id: int) -> bool:
    """Проверяет — есть ли уже открытая задача с нашим маркером."""
    status, resp = api(
        "GET",
        f"/tasks?filter[entity_type]=leads&filter[entity_id]={lead_id}&filter[is_completed]=0&limit=50",
    )
    if status != 200 or not resp:
        return False
    tasks = resp.get("_embedded", {}).get("tasks", [])
    for t in tasks:
        if TASK_TEXT in (t.get("text") or ""):
            return True
    return False


def create_task(lead_id: int, responsible_user_id: int):
    payload = [{
        "task_type_id": TASK_TYPE_ID,
        "text": TASK_TEXT,
        "complete_till": end_of_today_ts(),
        "entity_id": lead_id,
        "entity_type": "leads",
        "responsible_user_id": responsible_user_id,
    }]
    return api("POST", "/tasks", payload)


def process_pipeline(pipeline_id: int, cfg: dict) -> None:
    status, resp = api(
        "GET",
        f"/leads?filter[pipeline_id]={pipeline_id}&filter[statuses][0][pipeline_id]={pipeline_id}&filter[statuses][0][status_id]={cfg['status_kaspi_check']}&limit=50",
    )
    if status == 204 or not resp:
        return
    if status != 200:
        log(f"❌ GET {cfg['name']} failed: HTTP {status}")
        return
    leads = resp.get("_embedded", {}).get("leads", [])
    if not leads:
        return

    for lead in leads:
        if has_open_task(lead["id"]):
            continue
        responsible = lead.get("responsible_user_id") or 8282107  # fallback на main user
        st, r = create_task(lead["id"], responsible)
        if st in (200, 201, 202):
            log(f"  ✅ #{lead['id']} ({cfg['name']}): задача 'Проверь Kaspi-чек' создана для user {responsible}")
        else:
            log(f"  ❌ #{lead['id']} create task failed: HTTP {st}: {str(r)[:200]}")


def main() -> int:
    for pipeline_id, cfg in PIPELINES.items():
        process_pipeline(pipeline_id, cfg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"❌ Top-level error: {e}")
        sys.exit(2)
