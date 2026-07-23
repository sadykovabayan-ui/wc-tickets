#!/usr/bin/env python3
"""
Выдача билета: для каждой сделки в статусе «Оплачено» —
1. Генерирует уникальный номер билета (WCF26-ALA-0001 / WCF26-AST-0001).
2. Генерирует QR через api.qrserver.com (чёрный, размер большой).
3. Рендерит PDF из HTML-шаблона билета с подстановкой ФИО / даты / QR / номера.
4. Отправляет email с PDF во вложении через Resend.
5. Записывает в сделку: номер билета и ссылку на PDF.
6. Переводит сделку в статус «Билет отправлен».

Запускается через launchd каждые 2 минуты (см. wc.tickets.send_ticket.plist).
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import sys
import base64
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "token.local"             # fallback для локального dev
RESEND_KEY_FILE = SCRIPT_DIR / "resend.key.local"   # fallback для локального dev
LOG_FILE = SCRIPT_DIR / "send_ticket.log"
LOCK_FILE = SCRIPT_DIR / "send_ticket.lock"         # защита от параллельного запуска
PDF_DIR = SCRIPT_DIR / "pdfs"                       # сюда сохраняем сгенерированные PDF
TEMPLATE_FILE = SCRIPT_DIR / "ticket_template.html" # шаблон билета с плейсхолдерами

PDF_DIR.mkdir(exist_ok=True)


def _get_secret(env_var: str, file_path: Path) -> str:
    """GitHub Actions secret через env, fallback на локальный файл."""
    v = os.environ.get(env_var)
    if v:
        return v.strip()
    if file_path.exists():
        return file_path.read_text().strip()
    raise RuntimeError(f"Не найден секрет: ни env {env_var}, ни файл {file_path}")

API_BASE = "https://womancreate.amocrm.ru/api/v4"

# Конфигурация воронок
PIPELINES = {
    10952890: {  # Алматы 2026
        "name": "Фестиваль Алматы 2026",
        "city": "Алматы",
        "city_code": "ALA",
        "date_human": "15 августа 2026",
        "date_iso": "2026-08-15",
        "status_paid": 86119638,       # Оплачено
        "status_sent": 86119642,       # Билет отправлен
    },
    10952898: {  # Астана 2026
        "name": "Фестиваль Астана 2026",
        "city": "Астана",
        "city_code": "AST",
        "date_human": "22 августа 2026",
        "date_iso": "2026-08-22",
        "status_paid": 86119678,
        "status_sent": 86119682,
    },
}

# Поля сделки
FIELD_TICKET_NUMBER = 1173752       # Номер билета 2026 (text) — для N>1 храним все через ";"
FIELD_QR_LINK = 1166691             # Ссылка на QR билет (url)
FIELD_TICKET_QTY = 1173814          # Количество билетов (numeric) — заполняется dispatch или вручную
FIELD_PRODUCTS = 1166201            # PRODUCTS от Tilda Cart (тут "Купить билет - 4x15000 = 60000")
FIELD_GUEST_NAMES = 1173816         # Имена гостей (textarea) — по строке на гостя
FIELD_RESEND_EMAIL_ID = 1174381     # Resend email ID — для отслеживания bounce
# Email и ФИО — берутся из связанного контакта

# Resend
# До подтверждения домена womancreate.kz в Resend — отправляем с onboarding@resend.dev.
# После Verify — закомментируй первую строку и раскомментируй вторую (или переменная WC_PROD_EMAIL=1).
import os as _os
if _os.environ.get("WC_PROD_EMAIL") == "1":
    RESEND_FROM_EMAIL = "noreply@womancreate.kz"
else:
    RESEND_FROM_EMAIL = "onboarding@resend.dev"
RESEND_FROM_NAME = "Woman Create"
RESEND_REPLY_TO = "sadykova.bayan@gmail.com"     # сюда полетят все «Ответить» на письма с билетом
RESEND_TEST_TO = ""                              # боевой режим: письма идут реальным гостям (домен verified 27.05.2026)
WC_WHATSAPP = "+7 707 229 53 57"                 # телефон поддержки в подвале письма


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
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def next_ticket_number(city_code: str, pipeline_id: int) -> str:
    """Считает max существующий номер билета для города через AmoCRM, возвращает следующий.
    Парсит ВСЕ номера из любой записи (включая списки через ;), не только одиночные.
    Stateless — нет зависимости от локального counter-файла.
    """
    import re as _re
    max_n = 0
    page = 1
    while page <= 5:  # до 5 страниц по 250 = до 1250 сделок на воронку
        st, resp = amo("GET",
            f"/leads?filter[pipeline_id]={pipeline_id}&with=custom_fields_values&limit=250&page={page}")
        if st != 200 or not resp:
            break
        leads = resp.get("_embedded", {}).get("leads", []) or []
        if not leads:
            break
        for lead in leads:
            for cf in lead.get("custom_fields_values") or []:
                if cf.get("field_id") == FIELD_TICKET_NUMBER:
                    val = (cf.get("values") or [{}])[0].get("value", "") or ""
                    # findall парсит ВСЕ номера в строке. Поддерживает форматы:
                    #   "WCF26-ALA-0010"
                    #   "WCF26-ALA-0010; WCF26-ALA-0011; WCF26-ALA-0012"
                    #   "WCF26-ALA-0010, WCF26-ALA-0011"
                    nums = _re.findall(rf"WCF26-{city_code}-(\d+)", val)
                    for n_str in nums:
                        n = int(n_str)
                        if n > max_n:
                            max_n = n
        if len(leads) < 250:
            break
        page += 1
    return f"WCF26-{city_code}-{max_n + 1:04d}"


def render_pdf(template_html: str, output_pdf: Path, ticket_data: dict) -> bool:
    """Рендерит HTML→PDF через Chrome headless. Возвращает True при успехе."""
    # Подставляем переменные
    html = template_html
    for key, val in ticket_data.items():
        html = html.replace("{{" + key + "}}", str(val))

    # Сохраняем временный HTML
    tmp_html = output_pdf.with_suffix(".html")
    tmp_html.write_text(html, encoding="utf-8")

    # Кросс-платформенный путь к Chrome/Chromium (mac или Ubuntu VPS).
    # Можно переопределить через env WC_CHROME_BIN.
    chrome = os.environ.get("WC_CHROME_BIN")
    if not chrome:
        for candidate in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/snap/bin/chromium",
        ):
            if Path(candidate).exists():
                chrome = candidate
                break
    if not chrome:
        log("  ❌ Chrome/Chromium не найден. Установи chromium-browser или укажи WC_CHROME_BIN.")
        return False
    cmd = [
        chrome, "--headless", "--no-sandbox", "--disable-gpu",
        f"--print-to-pdf={output_pdf}",
        "--no-pdf-header-footer",
        "--virtual-time-budget=3000",
        f"file://{tmp_html}",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=60)
    if res.returncode != 0:
        log(f"  ❌ Chrome PDF render failed: {res.stderr.decode()[:200]}")
        return False
    return True


def resend_send(to_email: str, to_name: str, subject: str, html: str, pdf_paths):
    """Отправляет email через Resend API. pdf_paths — Path или список Path.
    Возвращает email_id (string) при успехе, None при ошибке.
    """
    try:
        api_key = _get_secret("RESEND_API_KEY", RESEND_KEY_FILE)
    except RuntimeError as e:
        log(f"  ⏸ {e} — пропускаю отправку")
        return None
    recipient = RESEND_TEST_TO or to_email

    # Inline-баннер в шапке письма (cid:wc_banner в HTML)
    attachments = []
    banner_path = SCRIPT_DIR / "wc_banner.jpg"
    if banner_path.exists():
        attachments.append({
            "filename": "wc_banner.jpg",
            "content": base64.b64encode(banner_path.read_bytes()).decode(),
            "content_id": "wc_banner",
            "content_type": "image/jpeg",
        })
    # Поддержка одиночного пути и списка
    pdf_list = pdf_paths if isinstance(pdf_paths, (list, tuple)) else [pdf_paths]
    for p in pdf_list:
        attachments.append({
            "filename": p.name,
            "content": base64.b64encode(p.read_bytes()).decode(),
        })
    payload = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [recipient],
        "reply_to": RESEND_REPLY_TO,
        "subject": subject,
        "html": html,
        "attachments": attachments,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "wc-tickets/1.0 (+https://womancreate.kz)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            data = json.loads(body) if body else {}
            email_id = data.get("id")
            log(f"  📧 Resend OK → {recipient}  (id={email_id})")
            return email_id
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        log(f"  ❌ Resend failed HTTP {e.code}: {body}")
        return None
    except urllib.error.URLError as e:
        log(f"  ❌ Resend network error (URLError): {e.reason}")
        return None
    except Exception as e:
        log(f"  ❌ Resend unknown error: {type(e).__name__}: {e}")
        return None


def get_contact(contact_id: int):
    st, resp = amo("GET", f"/contacts/{contact_id}")
    return resp if st == 200 else None


def extract_email_phone_name(contact: dict):
    name = contact.get("name", "") if contact else ""
    email = phone = ""
    for cf in (contact or {}).get("custom_fields_values") or []:
        code = (cf.get("field_code") or "").upper()
        if code == "EMAIL":
            email = (cf["values"][0]["value"] if cf.get("values") else "") or ""
        elif code == "PHONE":
            phone = (cf["values"][0]["value"] if cf.get("values") else "") or ""
    return name, email, phone


def _looks_like_name(line: str) -> bool:
    """Эвристика: строка похожа на ФИО (2+ слов кириллицей, нет цифр/email/спецсимволов)."""
    s = line.strip()
    if not s or len(s) < 4:
        return False
    if "@" in s or any(c.isdigit() for c in s):
        return False
    # Хотя бы 2 слова, начинающиеся с заглавной буквы
    words = [w for w in re.split(r"\s+", s) if w]
    if len(words) < 2:
        return False
    cap_words = sum(1 for w in words if w[:1].isupper())
    return cap_words >= 2


def extract_guest_names(lead: dict, lead_id: int, qty: int) -> list:
    """Возвращает список имён гостей. Источники по приоритету:
    1. FIELD_GUEST_NAMES (поле «Имена гостей», по строке/запятой)
    2. Последнее common-примечание в сделке (парсим строки которые похожи на ФИО)
    3. Пустой список → используется одно имя плательщика.
    """
    raw = ""
    for cf in lead.get("custom_fields_values") or []:
        if cf.get("field_id") == FIELD_GUEST_NAMES:
            raw = (cf.get("values") or [{}])[0].get("value", "") or ""
            break

    candidate_lines = []
    if raw:
        # Разделители: перенос строки или ;
        candidate_lines = re.split(r"[\n;]+", raw)
    else:
        # Парсим примечания (notes) сделки — берём самое свежее common-примечание
        st, resp = amo("GET", f"/leads/{lead_id}/notes?limit=20&order[updated_at]=desc")
        if st == 200 and resp:
            for note in (resp.get("_embedded", {}).get("notes") or []):
                if note.get("note_type") != "common":
                    continue
                text = (note.get("params") or {}).get("text", "") or ""
                if not text:
                    continue
                lines = re.split(r"[\n;]+", text)
                names = [ln for ln in lines if _looks_like_name(ln)]
                if names:
                    candidate_lines = names
                    break

    names = [ln.strip() for ln in candidate_lines if _looks_like_name(ln)]
    return names[:qty]  # не больше qty


def detect_ticket_qty(lead: dict) -> int:
    """Определяет количество билетов на сделку.
    Приоритет: FIELD_TICKET_QTY > PRODUCTS-парсинг (Tilda Cart) > 1.
    """
    cfs = lead.get("custom_fields_values") or []
    # 1) Явное поле «Количество билетов»
    for cf in cfs:
        if cf.get("field_id") == FIELD_TICKET_QTY:
            try:
                v = (cf.get("values") or [{}])[0].get("value")
                if v is not None:
                    n = int(v)
                    if 1 <= n <= 50:
                        return n
            except (ValueError, TypeError):
                pass
    # 2) Парсинг PRODUCTS (Tilda Cart): "Купить билет - 4x15000 = 60000"
    for cf in cfs:
        if cf.get("field_id") == FIELD_PRODUCTS:
            v = (cf.get("values") or [{}])[0].get("value", "") or ""
            m = re.search(r"(\d+)\s*[x×]\s*\d", v, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if 1 <= n <= 50:
                    return n
    # 3) Default — 1 билет
    return 1


def ticket_numbers_for_lead(lead: dict, pipe_cfg: dict, qty: int) -> list:
    """Возвращает список номеров билетов для сделки.
    Если в FIELD_TICKET_NUMBER уже что-то записано — парсит оттуда (защита от дублей).
    Иначе — генерит qty последовательных номеров через next_ticket_number.
    """
    existing = ""
    for cf in lead.get("custom_fields_values") or []:
        if cf.get("field_id") == FIELD_TICKET_NUMBER:
            existing = (cf.get("values") or [{}])[0].get("value", "") or ""
            break
    if existing:
        # парсим "WCF26-ALA-0010; WCF26-ALA-0011; ..." или с запятыми
        nums = re.findall(r"WCF26-[A-Z]{3}-\d{4}", existing)
        if nums:
            return nums
    # Генерим новые: первый через next_ticket_number, остальные инкрементом
    first = next_ticket_number(pipe_cfg["city_code"], pipe_cfg["_pipeline_id"])
    m = re.match(r"(WCF26-[A-Z]{3}-)(\d+)$", first)
    if not m:
        return [first]
    prefix, start = m.group(1), int(m.group(2))
    return [f"{prefix}{start + i:04d}" for i in range(qty)]


def process_lead(lead: dict, pipe_cfg: dict, template_html: str) -> None:
    lead_id = lead["id"]
    qty = detect_ticket_qty(lead)

    # ЗАЩИТА ОТ ДУБЛЕЙ #1: проверяем что записано в FIELD_TICKET_NUMBER.
    # Если там уже есть номера — значит билеты уже выпущены. Не шлём повторно.
    existing_raw = ""
    for cf in lead.get("custom_fields_values") or []:
        if cf.get("field_id") == FIELD_TICKET_NUMBER:
            existing_raw = (cf.get("values") or [{}])[0].get("value", "") or ""
            break

    if existing_raw:
        # Номера зарезервированы. Но было ли РЕАЛЬНО отправлено письмо?
        # Признак отправки — заполненный RESEND_EMAIL_ID. Если его нет —
        # прошлая попытка сорвалась (например, у контакта не было email),
        # и надо ПОВТОРИТЬ отправку с теми же номерами, а не двигать статус.
        has_email_id = False
        for cf in lead.get("custom_fields_values") or []:
            if cf.get("field_id") == FIELD_RESEND_EMAIL_ID:
                if (cf.get("values") or [{}])[0].get("value"):
                    has_email_id = True
                break
        if has_email_id:
            existing_nums = re.findall(r"WCF26-[A-Z]{3}-\d{4}", existing_raw)
            log(f"⏭ #{lead_id}: билеты уже отправлены ({len(existing_nums)} шт) — дотолкну статус, письмо НЕ повторяю")
            st, _ = amo("PATCH", f"/leads/{lead_id}", {"status_id": pipe_cfg["status_sent"]})
            if st in (200, 202):
                log(f"  ✅ #{lead_id} статус → «Билет отправлен»")
            else:
                log(f"  ⚠ #{lead_id} PATCH статуса вернул HTTP {st}")
            return
        log(f"🔁 #{lead_id}: номера зарезервированы ({existing_raw[:60]}), но письмо НЕ отправлялось — повторяю отправку")
        # НЕ выходим: продолжаем полный процесс. ticket_numbers_for_lead
        # переиспользует существующие номера — дублей не будет.

    log(f"📨 Сделка #{lead_id} «{lead['name']}» — генерирую {qty} билет(ов)")
    ticket_numbers = ticket_numbers_for_lead(lead, pipe_cfg, qty)
    ticket_numbers_str = "; ".join(ticket_numbers)

    # ЗАЩИТА ОТ ДУБЛЕЙ #2: СРАЗУ записываем все номера в AmoCRM.
    st, _ = amo("PATCH", f"/leads/{lead_id}", {
        "custom_fields_values": [
            {"field_id": FIELD_TICKET_NUMBER, "values": [{"value": ticket_numbers_str}]},
            {"field_id": FIELD_TICKET_QTY, "values": [{"value": qty}]},
        ]
    })
    if st not in (200, 202):
        log(f"  ❌ #{lead_id} не смог записать номера в AmoCRM (HTTP {st}) — отмена")
        return

    # Получаем email/имя/телефон из связанного контакта
    contact_id = None
    for c in (lead.get("_embedded") or {}).get("contacts") or []:
        if c.get("is_main"):
            contact_id = c["id"]
            break
    if not contact_id and (lead.get("_embedded") or {}).get("contacts"):
        contact_id = lead["_embedded"]["contacts"][0]["id"]

    if not contact_id:
        log(f"  ⚠ нет контакта у сделки — пропуск")
        return
    contact = get_contact(contact_id)
    name, email, phone = extract_email_phone_name(contact)
    if not email:
        log(f"  ⚠ у контакта нет email — пропуск")
        return
    if not name:
        name = "Гостья"

    # Имена гостей (если есть N имён — каждое попадёт на свой билет; иначе всё на плательщика)
    guest_names = extract_guest_names(lead, lead_id, qty) if qty > 1 else []
    if guest_names:
        log(f"  👥 #{lead_id}: найдено {len(guest_names)} имён гостей: {', '.join(guest_names)}")

    pdf_paths = []
    for idx, tn in enumerate(ticket_numbers, start=1):
        # Если есть отдельное имя гостя для этой позиции — используем его, иначе имя плательщика
        ticket_fio = guest_names[idx - 1] if idx - 1 < len(guest_names) else name
        ticket_surname = ticket_fio.split()[-1] if ticket_fio else "Гость"
        ticket_safe = re.sub(r"[^\w-]", "_", ticket_surname)
        pdf_path = PDF_DIR / f"Билет_{tn}_{ticket_safe}.pdf"
        # Метка «Билет 2 из 4» появляется только при N>1
        ticket_index = f"Билет {idx} из {qty}" if qty > 1 else ""
        ticket_data = {
            "FIO": ticket_fio,
            "DATE_HUMAN": pipe_cfg["date_human"],
            "CITY": pipe_cfg["city"],
            "LOCATION": "локация уточняется",
            "TICKET_NUMBER": tn,
            "TICKET_NUMBER_FORMATTED": tn.replace("-", "<span class='sep'>·</span>"),
            "TICKET_INDEX": ticket_index,
            "QR_URL": f"https://api.qrserver.com/v1/create-qr-code/?data={urllib.parse.quote(tn)}&size=600x600&ecc=H&color=000000&bgcolor=FFFFFF&margin=0",
        }
        if not render_pdf(template_html, pdf_path, ticket_data):
            log(f"  ❌ Не смог отрендерить PDF {tn} — отмена")
            return
        pdf_paths.append(pdf_path)

    # Email — одно письмо со всеми N вложениями
    if qty == 1:
        subject = f"Ваш билет на фестиваль «Я боюсь и делаю!» — {pipe_cfg['city']}"
        ticket_word = "Ваш билет"
        intro_line = f"Ваш билет на Женский Бизнес Фестиваль <strong>«Я боюсь и делаю!»</strong> во вложении."
    else:
        subject = f"Ваши билеты ({qty} шт) на фестиваль «Я боюсь и делаю!» — {pipe_cfg['city']}"
        ticket_word = f"Ваши {qty} билета"
        intro_line = f"Во вложении <strong>{qty} билета</strong> на Женский Бизнес Фестиваль <strong>«Я боюсь и делаю!»</strong>. У каждого свой уникальный QR — на входе у каждого гостя свой билет."

    numbers_html = "<br>".join(f"🎟 <strong>{tn}</strong>" for tn in ticket_numbers)

    body_html = f"""
    <div style="font-family: -apple-system, Arial, sans-serif; color: #1a1a1a; max-width: 600px; margin: 0 auto;">
      <img src="cid:wc_banner" alt="Woman Create — Я боюсь и делаю" style="width:100%; max-width:600px; display:block; border-radius:6px; margin-bottom:18px;">
      <p>Здравствуйте, {name}!</p>
      <p>{intro_line}</p>
      <p>
        📅 <strong>{pipe_cfg['date_human']}</strong><br>
        📍 <strong>{pipe_cfg['city']}</strong>, локация уточняется
      </p>
      <p>{numbers_html}</p>
      <p>На входе достаточно показать PDF с экрана телефона или распечатать.</p>
      <p style="color:#666;font-size:0.92em;">Возврат билетов возможен не позднее, чем за 72 часа до фестиваля.</p>
      <p>До встречи на фестивале!<br>С теплом, команда Woman Create</p>
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0 14px;">
      <p style="color:#888;font-size:0.85em;line-height:1.5;">
        Это письмо отправлено автоматически — отвечать на него не нужно.<br>
        По любым вопросам пишите нам в WhatsApp: <a href="https://wa.me/77072295357" style="color:#8a1538;text-decoration:none;"><strong>{WC_WHATSAPP}</strong></a>
      </p>
    </div>
    """
    email_id = resend_send(email, name, subject, body_html, pdf_paths)

    if email_id:
        # QR-ссылка — на первый билет + RESEND_EMAIL_ID для последующей bounce-проверки
        qr_link_first = f"https://api.qrserver.com/v1/create-qr-code/?data={urllib.parse.quote(ticket_numbers[0])}&size=600x600&ecc=H&color=000000&bgcolor=FFFFFF&margin=0"
        st, _ = amo("PATCH", f"/leads/{lead['id']}", {
            "custom_fields_values": [
                {"field_id": FIELD_QR_LINK, "values": [{"value": qr_link_first}]},
                {"field_id": FIELD_RESEND_EMAIL_ID, "values": [{"value": email_id}]},
            ],
            "status_id": pipe_cfg["status_sent"],
        })
        if st in (200, 202):
            log(f"  ✅ #{lead['id']} {qty} билет(ов) отправлено → «Билет отправлен» (email_id={email_id})")
        else:
            log(f"  ⚠ #{lead['id']} письмо ушло, PATCH статуса HTTP {st} — дотолкнётся на след. тике")
    else:
        log(f"  ⚠ #{lead['id']}: письмо НЕ ушло, номера {ticket_numbers_str} зарезервированы. Нужен ручной разбор.")


def main() -> int:
    # ЗАЩИТА ОТ ПАРАЛЛЕЛЬНОГО ЗАПУСКА (flock).
    # Если предыдущий запуск ещё не завершился (Chrome render долгий, Resend медленный) —
    # второй экземпляр сразу выходит, чтобы не отправить дубль билета.
    import fcntl
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("⏸ Предыдущий запуск ещё работает (lock занят) — выхожу, чтобы не было дублей")
        return 0
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()

    if not TEMPLATE_FILE.exists():
        log(f"❌ Нет шаблона билета: {TEMPLATE_FILE}")
        return 1
    template_html = TEMPLATE_FILE.read_text(encoding="utf-8")

    for pipeline_id, pipe_cfg in PIPELINES.items():
        pipe_cfg["_pipeline_id"] = pipeline_id  # для next_ticket_number
        # Ищем сделки в статусе «Оплачено» в этой воронке
        status, resp = amo(
            "GET",
            f"/leads?filter[pipeline_id]={pipeline_id}&filter[statuses][0][pipeline_id]={pipeline_id}&filter[statuses][0][status_id]={pipe_cfg['status_paid']}&with=contacts,custom_fields_values&limit=50",
        )
        if status == 204:
            continue
        if status != 200 or not resp:
            log(f"❌ GET {pipe_cfg['name']} failed: HTTP {status}")
            continue
        leads = resp.get("_embedded", {}).get("leads", [])
        if not leads:
            continue
        log(f"=== {pipe_cfg['name']}: {len(leads)} сделок в Оплачено ===")
        for lead in leads:
            try:
                process_lead(lead, pipe_cfg, template_html)
            except Exception as e:
                import traceback
                log(f"  ❌ #{lead['id']} Unhandled: {e}\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"❌ Top-level error: {e}")
        sys.exit(2)
