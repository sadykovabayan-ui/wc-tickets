# wc-tickets

Система автоматизации билетов фестиваля Woman Create «Я боюсь и делаю!» 2026.

## Архитектура

Три скрипта, запускаются GitHub Actions cron каждые 5 минут:

1. **move_astana.py** (`dispatch.yml`) — раскидывает заявки из «Основной» и «Фестиваль» воронок AmoCRM по городам Алматы 2026 / Астана 2026 на основе FORMID (Tilda) и PAYMENTID (Robokassa).
2. **send_ticket.py** (`send-ticket.yml`) — для сделок в статусе «Оплачено» генерирует PDF-билет (через headless Chrome), отправляет email через Resend, обновляет статус.
3. **kaspi_task.py** (`kaspi-task.yml`) — создаёт задачу менеджеру для проверки Kaspi-чека.

## Секреты

В Settings → Secrets and variables → Actions:
- `AMOCRM_TOKEN` — long-lived AmoCRM API token (Bearer)
- `RESEND_API_KEY` — Resend API key для отправки писем

## Локальный запуск (dev)

```bash
AMOCRM_TOKEN=xxx RESEND_API_KEY=yyy WC_PROD_EMAIL=1 python3 send_ticket.py
```

Или положи `token.local` и `resend.key.local` рядом со скриптом — они будут использованы как fallback.

## Архитектурные решения

- **Stateless ticket counter**: следующий номер билета считается из max-номера в AmoCRM (не из локального файла).
- **Защита от дублей**: ticket_number записывается в AmoCRM ДО отправки письма. Если повтор — увидим existing_number и не повторим отправку.
- **Lock-файл** (`fcntl.flock`) — защита от параллельного запуска.
- **Кросс-платформенный Chrome path** — работает на macOS и Ubuntu (GitHub Actions runner).
