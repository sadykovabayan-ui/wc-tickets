/**
 * WC Fest 2026 — Telegram-бот сканера билетов на входе.
 * Хостинг: Google Apps Script Web App (deploy: Execute as Me, Access: Anyone).
 *
 * Script Properties (Project Settings → Script properties):
 *   BOT_TOKEN     — токен бота от @BotFather
 *   AMOCRM_TOKEN  — long-lived токен AmoCRM
 *   ACCESS_CODE   — код доступа для волонтёров (например: wc26)
 *
 * После деплоя выполнить один раз функцию setWebhook() из редактора.
 *
 * Поток:
 *   Волонтёр: /start → вводит код доступа → получает кнопку «📷 Сканировать»
 *   Кнопка открывает mini app (GitHub Pages) → нативный QR-сканер Telegram
 *   → номер билета приходит боту → проверка в AmoCRM → ✅/❌ за ~2 секунды.
 *   Ручной ввод номера текстом тоже работает (если камера не читает).
 */

var AMO = 'https://womancreate.amocrm.ru/api/v4';
var SCANNER_URL = 'https://sadykovabayan-ui.github.io/wc-tickets/scanner.html';

var PIPES = {
  10952890: { city: 'Алматы', paid: 86119638, sent: 86119642 },
  10952898: { city: 'Астана', paid: 86119678, sent: 86119682 }
};
var F_TICKET = 1173752;   // Номер билета 2026
var F_SCANTIME = 1173762; // Время сканирования билета (date_time)
var F_VOLUNTEER = 1173764;// Волонтёр сканировал (text) — список отсканированных номеров
var STATUS_DONE = 142;    // Успешно реализовано (гость пришла)

function prop(k) { return PropertiesService.getScriptProperties().getProperty(k); }

// ===================== TELEGRAM =====================

function tgApi(method, payload) {
  var url = 'https://api.telegram.org/bot' + prop('BOT_TOKEN') + '/' + method;
  return UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), muteHttpExceptions: true
  });
}

function send(chatId, text, keyboard) {
  var p = { chat_id: chatId, text: text, parse_mode: 'HTML' };
  if (keyboard) p.reply_markup = keyboard;
  tgApi('sendMessage', p);
}

function scanKeyboard() {
  return {
    keyboard: [[{ text: '📷 Сканировать билет', web_app: { url: SCANNER_URL } }],
               [{ text: '📊 Статистика' }]],
    resize_keyboard: true, is_persistent: true
  };
}

// Один раз после деплоя: выполнить из редактора Apps Script
function setWebhook() {
  var url = ScriptApp.getService().getUrl();
  var r = tgApi('setWebhook', { url: url, allowed_updates: ['message'] });
  Logger.log(r.getContentText());
}

// ===================== AMOCRM =====================

function amo(method, path, body) {
  var r = UrlFetchApp.fetch(AMO + path, {
    method: method, contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + prop('AMOCRM_TOKEN') },
    payload: body ? JSON.stringify(body) : undefined,
    muteHttpExceptions: true
  });
  var code = r.getResponseCode();
  var text = r.getContentText();
  return { code: code, json: text ? JSON.parse(text) : null };
}

function cf(lead, fieldId) {
  var cfs = (lead.custom_fields_values || []);
  for (var i = 0; i < cfs.length; i++) {
    if (cfs[i].field_id === fieldId) {
      var vals = cfs[i].values || [];
      return vals.length ? String(vals[0].value || '') : '';
    }
  }
  return '';
}

function findLeadByTicket(num) {
  var r = amo('get', '/leads?query=' + encodeURIComponent(num) + '&with=contacts,custom_fields_values&limit=10');
  if (r.code !== 200 || !r.json) return null;
  var leads = (r.json._embedded || {}).leads || [];
  for (var i = 0; i < leads.length; i++) {
    var tickets = cf(leads[i], F_TICKET).toUpperCase();
    if (tickets.indexOf(num) !== -1) return leads[i];
  }
  return null;
}

function guestName(lead) {
  try {
    var cs = (lead._embedded || {}).contacts || [];
    if (!cs.length) return lead.name || '';
    var r = amo('get', '/contacts/' + cs[0].id);
    return (r.json && r.json.name) ? r.json.name : (lead.name || '');
  } catch (e) { return lead.name || ''; }
}

// ===================== ПРОВЕРКА БИЛЕТА =====================

function checkTicket(num, volunteer) {
  num = num.toUpperCase();
  var lead = findLeadByTicket(num);
  if (!lead) return '❌ <b>БИЛЕТ НЕ НАЙДЕН</b>\n<code>' + num + '</code>\nПроверь номер или зови старшего.';

  var pipe = PIPES[lead.pipeline_id];
  if (!pipe) return '⚠️ <b>СТОП</b>\nБилет <code>' + num + '</code> найден в служебной воронке. Зови старшего.';

  // Разрешённые статусы для прохода: Билет отправлен / Оплачено / уже Успешно (частично отсканированный заказ)
  var okStatuses = [pipe.sent, pipe.paid, STATUS_DONE];
  if (okStatuses.indexOf(lead.status_id) === -1) {
    return '⚠️ <b>СТОП — оплата не подтверждена</b>\n<code>' + num + '</code>\nСтатус сделки не «Билет отправлен». Зови старшего.';
  }

  var allTickets = (cf(lead, F_TICKET).toUpperCase().match(/WCF26-(?:ALA|AST)-\d{4}/g)) || [num];
  var scannedRaw = cf(lead, F_VOLUNTEER);           // формат: "0049@10:23;0050@10:31"
  var shortNum = num.slice(-4);
  if (scannedRaw.indexOf(shortNum + '@') !== -1) {
    var t = scannedRaw.split(shortNum + '@')[1].split(';')[0];
    return '🚫 <b>УЖЕ ИСПОЛЬЗОВАН</b>\n<code>' + num + '</code>\nСканирован сегодня в <b>' + t + '</b>.\nНе пропускать! Зови старшего.';
  }

  // Отмечаем
  var now = new Date();
  var hhmm = Utilities.formatDate(now, 'Asia/Almaty', 'HH:mm');
  var newScanned = scannedRaw ? scannedRaw + ';' + shortNum + '@' + hhmm : shortNum + '@' + hhmm;
  var scannedCount = newScanned.split(';').length;
  var allDone = scannedCount >= allTickets.length;

  var fields = [
    { field_id: F_VOLUNTEER, values: [{ value: newScanned }] }
  ];
  if (!scannedRaw) fields.push({ field_id: F_SCANTIME, values: [{ value: Math.floor(now.getTime() / 1000) }] });
  var patch = { custom_fields_values: fields };
  if (allDone) patch.status_id = STATUS_DONE;
  amo('patch', '/leads/' + lead.id, patch);

  // Примечание — кто сканировал (в поле не влезает, пишем в историю)
  amo('post', '/leads/' + lead.id + '/notes',
      [{ note_type: 'common', params: { text: '🎟 Скан ' + num + ' в ' + hhmm + ' волонтёром: ' + volunteer } }]);

  var name = guestName(lead);
  var extra = allTickets.length > 1
    ? '\n👥 Заказ на ' + allTickets.length + ' билетов, отсканировано: ' + scannedCount + ' из ' + allTickets.length
    : '';
  return '✅ <b>ПРОХОДИТ</b>\n<b>' + name + '</b>\n<code>' + num + '</code> · ' + pipe.city + ' · ' + hhmm + extra;
}

// ===================== СТАТИСТИКА =====================

function stats() {
  var out = '📊 <b>Статистика входа</b>\n';
  var pipeIds = Object.keys(PIPES);
  for (var i = 0; i < pipeIds.length; i++) {
    var pid = pipeIds[i];
    var scanned = 0, totalTickets = 0, page = 1;
    while (page <= 4) {
      var r = amo('get', '/leads?filter[pipeline_id]=' + pid + '&with=custom_fields_values&limit=250&page=' + page);
      if (r.code !== 200 || !r.json) break;
      var leads = (r.json._embedded || {}).leads || [];
      if (!leads.length) break;
      for (var j = 0; j < leads.length; j++) {
        var tks = (cf(leads[j], F_TICKET).match(/WCF26/g) || []).length;
        totalTickets += tks;
        var sc = cf(leads[j], F_VOLUNTEER);
        if (sc) scanned += sc.split(';').length;
      }
      if (leads.length < 250) break;
      page++;
    }
    out += '\n<b>' + PIPES[pid].city + '</b>: прошло ' + scanned + ' из ' + totalTickets + ' билетов';
  }
  return out;
}

// ===================== ДОСТУП ВОЛОНТЁРОВ =====================

function isAuthorized(chatId) {
  return PropertiesService.getScriptProperties().getProperty('VOL_' + chatId) !== null;
}
function authorize(chatId, name) {
  PropertiesService.getScriptProperties().setProperty('VOL_' + chatId, name || 'волонтёр');
}
function volunteerName(chatId) {
  return PropertiesService.getScriptProperties().getProperty('VOL_' + chatId) || 'волонтёр';
}

// ===================== WEBHOOK =====================

function doPost(e) {
  try {
    var upd = JSON.parse(e.postData.contents);
    var msg = upd.message;
    if (!msg) return ContentService.createTextOutput('ok');
    var chatId = msg.chat.id;
    var fromName = ((msg.from || {}).first_name || '') + ' ' + ((msg.from || {}).last_name || '');
    fromName = fromName.trim() || ('id' + chatId);

    // Данные из mini app сканера
    if (msg.web_app_data && msg.web_app_data.data) {
      if (!isAuthorized(chatId)) { send(chatId, '🔒 Сначала введи код доступа.'); return ContentService.createTextOutput('ok'); }
      send(chatId, checkTicket(msg.web_app_data.data, volunteerName(chatId)), scanKeyboard());
      return ContentService.createTextOutput('ok');
    }

    var text = (msg.text || '').trim();

    if (text === '/start') {
      if (isAuthorized(chatId)) {
        send(chatId, 'С возвращением! Жми кнопку и сканируй 🎟', scanKeyboard());
      } else {
        send(chatId, '👋 Привет! Это сканер билетов фестиваля «Я боюсь и делаю!»\n\n🔒 Введи <b>код доступа</b> (спроси у организатора):');
      }
      return ContentService.createTextOutput('ok');
    }

    // Код доступа
    if (!isAuthorized(chatId)) {
      if (text.toLowerCase() === String(prop('ACCESS_CODE') || '').toLowerCase()) {
        authorize(chatId, fromName);
        send(chatId, '✅ Доступ открыт, ' + fromName + '!\nЖми кнопку — откроется сканер.', scanKeyboard());
      } else {
        send(chatId, '❌ Неверный код. Попробуй ещё раз.');
      }
      return ContentService.createTextOutput('ok');
    }

    if (text === '📊 Статистика' || text === '/stats') {
      send(chatId, stats(), scanKeyboard());
      return ContentService.createTextOutput('ok');
    }

    // Ручной ввод номера билета
    var m = text.toUpperCase().match(/WCF26-(?:ALA|AST)-\d{4}/);
    if (m) {
      send(chatId, checkTicket(m[0], volunteerName(chatId)), scanKeyboard());
      return ContentService.createTextOutput('ok');
    }

    send(chatId, 'Не понял 🤔 Жми «📷 Сканировать билет» или пришли номер билета (WCF26-ALA-0001).', scanKeyboard());
  } catch (err) {
    // Никогда не роняем webhook
  }
  return ContentService.createTextOutput('ok');
}
