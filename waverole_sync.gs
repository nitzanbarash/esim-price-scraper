/**
 * Waverole ↔ Google Sheet sync — STANDALONE Apps Script project.
 *
 * Why standalone: the spreadsheet sits in shared storage whose security
 * restrictions block creating a container-bound script ("מגבלות אבטחה").
 * A standalone project + installable triggers works around that: it opens
 * the sheet by ID, so no binding is needed. Limitation: standalone scripts
 * cannot add a custom menu inside the sheet — manual actions run from the
 * Apps Script editor (Run ▶) instead.
 *
 * What it does:
 *  1. INSTANT site update whenever a relevant cell is edited in the sheet
 *     (installable onEdit trigger). NOTE: programmatic writes (the daily
 *     scraper) do NOT fire onEdit — that's what the daily full sync is for.
 *  2. Daily 10:00 Israel: starts the GitHub scraper, then a full site sync
 *     45 minutes later (after the scrape finished writing fresh data).
 *  3. Daily 12:00 Israel: WATCHDOG — verifies the live site data is fresh;
 *     emails ALERT_EMAIL if the site wasn't updated in the last 26 hours.
 *  4. Any failure (missing column, HTTP error, exception) emails ALERT_EMAIL
 *     instead of failing silently.
 *
 * One-time setup (in the Apps Script editor, script.google.com):
 *  1. Paste this file over Code.gs → Save (Cmd+S).
 *  2. Project Settings (⚙) → Script properties → add:
 *       UPDATE_PACKAGES_TOKEN = the site's UPDATE_PACKAGES_TOKEN
 *                      (was called SITE_TOKEN here; both names still work)
 *       GH_TOKEN     = GitHub PAT (repo+workflow) for esim-price-scraper
 *       ORDERS_TOKEN = the site's ORDERS_TOKEN — the SAME value the PC bot
 *                      and GitHub Actions already use. There is only ever
 *                      ONE of these: the site checks one string, so every
 *                      client presents that same string. Never mint a second
 *                      one — it is also the HMAC key that signs the payment
 *                      callback, so a mismatch silently rejects real orders.
 *                      Optional here; without it the 1-min fulfillment tick
 *                      cannot see a paid order until the PC bot reports it.
 *  3. In the editor pick `setupTriggers` in the function dropdown → Run ▶
 *     → authorize when prompted. Done.
 *
 * Manual actions (function dropdown → Run ▶):
 *   previewLog     — log the exact JSON that would be sent (dry run)
 *   fullSync       — push all packages to the site now
 *   runScrapeNow   — trigger the GitHub scraper now
 *   checkSiteFresh — run the freshness watchdog now
 */

const ENDPOINT = 'https://www.waverole.com/api/update-packages';
const OVERLAY_URL = 'https://www.waverole.com/data/plans-overlay.json';
const GH_DISPATCH = 'https://api.github.com/repos/nitzanbarash/esim-price-scraper/actions/workflows/scrape.yml/dispatches';
const FULFILL_DISPATCH = 'https://api.github.com/repos/nitzanbarash/esim-price-scraper/actions/workflows/fulfillment.yml/dispatches';
const SHEET_ID = '108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo';
const RECEIPTS_ID = '1bWH_Zef0aNwZjLOR07hjJRZRXkrY73mX0aMLGPH6uao';
const ALERT_EMAIL = 'uper.request@gmail.com';
const MAX_STALE_HOURS = 26;   // watchdog: alert if site data older than this
const BACKUP_FOLDER = 'Waverole Backups';   // Drive folder for weekly copies
const BACKUP_KEEP = 8;                      // copies kept per spreadsheet

// Row-1 header text (trimmed) → API field.
// Each field lists EVERY name the column has ever had, so renaming a header
// doesn't silently break the sync again (2026-07-09: 'כולל מעמ' → 'מחיר סופי'
// went unnoticed and price updates stopped reaching the site).
const HEADERS = {
  sku:         ['חבילה (קוד)'],
  gb:          ['GB'],
  days:        ['זמן חבילה'],
  networks:    ['Networks'],
  breakout_ip: ['Breakout IP'],
  stock:       ['במלאי/רווחי'],              // empty = in stock
  fee:         ['סליקה'],
  price:       ['מחיר סופי', 'כולל מעמ'],    // FINAL customer price (incl. VAT + fee)
  sale:        ['מבעצעים (אחוזים)'],         // empty/0 cancels the sale
};
// Fields the sync cannot work without — missing => loud email, not silence.
const REQUIRED_FIELDS = ['sku', 'price'];

function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('onEditPush')
    .forSpreadsheet(SHEET_ID).onEdit().create();
  // Same idea for the receipts sheet: a consumption figure edited there
  // reaches the customer's usage meter within seconds.
  ScriptApp.newTrigger('onReceiptsEdit')
    .forSpreadsheet(RECEIPTS_ID).onEdit().create();
  ScriptApp.newTrigger('dailyScrape').timeBased()
    .atHour(10).everyDays(1).inTimezone('Asia/Jerusalem').create();
  ScriptApp.newTrigger('checkSiteFresh').timeBased()
    .atHour(12).everyDays(1).inTimezone('Asia/Jerusalem').create();
  // GitHub throttles */5 cron on public repos to ~1/hour in practice, so the
  // fulfillment bot is dispatched from here instead. Fires every minute; the
  // handler itself decides whether to dispatch — every minute while an order
  // waits for its eSIM, every 5th minute otherwise. Needs GH_TOKEN (skips
  // without it).
  ScriptApp.newTrigger('fulfillmentTick').timeBased().everyMinutes(1).create();
  // Weekly Drive copies of both spreadsheets — the sheets ARE the business
  // (prices, receipts, eSIM codes); an accidental mass-delete or a broken
  // formula paste would otherwise be unrecoverable beyond version history.
  ScriptApp.newTrigger('weeklyBackup').timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY).atHour(3).inTimezone('Asia/Jerusalem').create();
  Logger.log('Triggers installed: onEdit sync + daily 10:00 scrape + 12:00 watchdog + 1-min fulfillment tick + weekly backup');
}

// ── helpers ─────────────────────────────────────────────────────────
function alert_(subject, body) {
  try {
    MailApp.sendEmail(ALERT_EMAIL, '⚠️ Waverole sync: ' + subject,
      body + '\n\n(הודעה אוטומטית מסקריפט הסנכרון של טבלת המחירים)');
  } catch (e) { Logger.log('alert email failed: ' + e); }
}

// Positive daily confirmation — sent when the morning check passed, so a
// silent inbox never leaves you guessing whether the check ran at all.
function report_(subject, body) {
  try {
    MailApp.sendEmail(ALERT_EMAIL, '✅ Waverole sync: ' + subject,
      body + '\n\n(הודעה אוטומטית מסקריפט הסנכרון של טבלת המחירים)');
  } catch (e) { Logger.log('report email failed: ' + e); }
}

function sheet_() {
  return SpreadsheetApp.openById(SHEET_ID).getSheets()[0];
}

function colMap_(sheet) {
  const head = sheet.getRange(1, 1, 1, sheet.getLastColumn())
    .getValues()[0].map(h => String(h).trim());
  const map = {};
  for (const [key, names] of Object.entries(HEADERS)) {
    for (const name of names) {
      const i = head.indexOf(name);
      if (i >= 0) { map[key] = i; break; }             // 0-based
    }
  }
  const missing = REQUIRED_FIELDS.filter(f => map[f] === undefined);
  if (missing.length) {
    const msg = 'עמודות חסרות בטבלת המחירים: ' + missing.join(', ') +
      '\nכנראה שונה שם של כותרת. שמות שהסקריפט מכיר: ' +
      missing.map(f => HEADERS[f].join(' / ')).join(' | ') +
      '\nיש לעדכן את HEADERS בקוד או להחזיר את שם העמודה.';
    alert_('עמודה חסרה — הסנכרון נעצר', msg);
    throw new Error(msg);
  }
  return map;
}

function num_(v) {
  const n = parseFloat(String(v).replace(/[^\d.]/g, ''));
  return isNaN(n) ? null : n;
}

function rowToPackage_(row, map) {
  const sku = String(row[map.sku] || '').trim();
  if (!sku || sku.indexOf('.') < 0) return null;    // not a package row
  const pkg = { sku: sku };
  const price = num_(row[map.price]);
  if (price !== null) pkg.price = price;
  pkg.sale = num_(row[map.sale]) || 0;
  pkg.in_stock = String(row[map.stock] || '').trim() === '';
  const days = num_(row[map.days]); if (days !== null) pkg.days = days;
  const gb   = num_(row[map.gb]);   if (gb   !== null) pkg.gb = gb;
  const net = String(row[map.networks] || '').replace(/^Networks\s*•\s*/i, '').trim();
  if (net) pkg.networks = net;
  const bip = String(row[map.breakout_ip] || '').trim();
  if (bip) pkg.breakout_ip = bip;
  const fee = num_(row[map.fee]); if (fee !== null) pkg.fee = fee;
  return pkg;
}

function buildPackages_(rowsWanted) {   // rowsWanted: null = all, or Set of sheet row numbers
  const sheet = sheet_();
  const map = colMap_(sheet);
  const data = sheet.getDataRange().getValues();
  const out = [];
  for (let r = 1; r < data.length; r++) {
    if (rowsWanted && !rowsWanted.has(r + 1)) continue;
    const pkg = rowToPackage_(data[r], map);
    if (pkg) out.push(pkg);
  }
  return out;
}

function post_(packages) {
  // Same secret the site calls UPDATE_PACKAGES_TOKEN. It was originally added
  // here under the name SITE_TOKEN, and one secret wearing two names is how
  // you end up unable to tell which key is which. Prefer the site's name;
  // keep reading the old one so the existing property keeps working.
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('UPDATE_PACKAGES_TOKEN') || props.getProperty('SITE_TOKEN');
  if (!token) throw new Error('חסר UPDATE_PACKAGES_TOKEN ב-Script Properties (הגדרות הפרויקט)');
  const res = UrlFetchApp.fetch(ENDPOINT, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token },
    payload: JSON.stringify({ packages: packages }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  const body = res.getContentText();
  Logger.log(code + ' ' + body);
  if (code >= 300) {
    alert_('שליחת עדכון לאתר נכשלה (HTTP ' + code + ')',
      'הקריאה ל-' + ENDPOINT + ' החזירה ' + code + ':\n' + body.slice(0, 500));
    throw new Error('update-packages HTTP ' + code);
  }
  let msg = 'HTTP ' + code;
  try {
    const j = JSON.parse(body);
    msg = 'עודכנו ' + (j.updated || []).length +
      ((j.not_found || []).length ? ' | לא נמצאו: ' + j.not_found.join(', ') : '') +
      ((j.warnings || []).length ? ' | ⚠️ ' + j.warnings.length + ' אזהרות' : '');
  } catch (err) {}
  Logger.log(msg);
  // Freshness signal for the watchdog: a successful POST means the site HAS
  // today's prices even when nothing changed (the endpoint then skips the
  // commit, so the overlay's `updated` timestamp does NOT move — that false
  // alarm is exactly what fired on 2026-07-16).
  PropertiesService.getScriptProperties()
    .setProperty('LAST_SYNC_OK', new Date().toISOString());
  try { SpreadsheetApp.openById(SHEET_ID).toast(msg, 'Waverole', 8); } catch (e) {}
  return body;
}

// ── actions ─────────────────────────────────────────────────────────
// Editing a run of cells used to fire one full site push PER EDIT. A few
// minutes of ordinary work on the sheet produced 28 overlapping runs, one of
// which hung for 175 seconds — and because Apps Script caps how much runs at
// once, that flood starved the every-minute fulfilment dispatcher, which is
// what makes a customer's eSIM late.
//
// So an edit now queues its row and tries to flush straight away. If another
// flush already holds the lock it just leaves the row queued: whoever is
// flushing, or the next minute tick, will send it. Isolated edits stay
// instant, a burst collapses into one push, and nothing piles up.
const PENDING_ROWS_KEY = 'PENDING_SYNC_ROWS';

function onEditPush(e) {
  try {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet();
    const main = e.source.getSheets()[0];
    if (sheet.getSheetId() !== main.getSheetId()) return;
    const map = colMap_(sheet);
    const watched = Object.values(map).map(i => i + 1);
    const c1 = e.range.getColumn(), c2 = e.range.getLastColumn();
    if (!watched.some(c => c >= c1 && c <= c2)) return;   // not a synced column
    const rows = [];
    for (let r = Math.max(2, e.range.getRow()); r <= e.range.getLastRow(); r++) rows.push(r);
    if (!rows.length) return;
    queueRows_(rows);
    flushPendingRows_();
  } catch (err) {
    // colMap_/post_ already emailed the specific reason; log and stop.
    Logger.log('onEditPush failed: ' + err);
  }
}

function queueRows_(rows) {
  const props = PropertiesService.getScriptProperties();
  const raw = props.getProperty(PENDING_ROWS_KEY) || '';
  if (raw === 'ALL') return;                  // already sending everything
  const have = raw.split(',').filter(Boolean);
  const all = [...new Set(have.concat(rows.map(String)))];
  // Script Properties cap a value at 9 KB. Thousands of queued rows means
  // something is rewriting the whole sheet anyway, so fall back to a full
  // sync rather than dropping rows silently.
  props.setProperty(PENDING_ROWS_KEY, all.length > 800 ? 'ALL' : all.join(','));
}

/** Send whatever is queued. Returns how many packages went out. */
function flushPendingRows_() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return 0;          // someone else is already sending
  try {
    const props = PropertiesService.getScriptProperties();
    const raw = props.getProperty(PENDING_ROWS_KEY) || '';
    if (!raw) return 0;
    props.deleteProperty(PENDING_ROWS_KEY);   // claim them before the slow part
    const pkgs = raw === 'ALL'
      ? buildPackages_(null)
      : buildPackages_(new Set(raw.split(',').filter(Boolean).map(Number)));
    if (pkgs.length) post_(pkgs);
    return pkgs.length;
  } catch (err) {
    Logger.log('flushPendingRows_ failed: ' + err);
    return 0;
  } finally {
    lock.releaseLock();
  }
}

// ── receipts sheet → customer's usage meter, instantly ──────────────
// The daily usage bot refreshes every live package once a day. This closes
// the gap in between: edit the consumption cell in the receipts sheet and the
// customer's order page shows the new figure within seconds — the same
// arrangement the price sheet has with the shop.
//
// Only MANUAL edits reach here; Apps Script does not fire onEdit for writes
// made by a script, so the daily bot is not double-counted (it pushes to the
// site directly anyway).
const RCPT_USAGE_COL = 'GB (0/X) - ניצול';
const RCPT_ORDER_COL = 'מס׳ הזמנה';

function onReceiptsEdit(e) {
  try {
    if (!e || !e.range) return;
    const sh = e.range.getSheet();
    const hdr = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].map(h => String(h).trim());
    const usageCol = hdr.indexOf(RCPT_USAGE_COL) + 1;
    const orderCol = hdr.indexOf(RCPT_ORDER_COL) + 1;
    if (!usageCol || !orderCol) return;
    if (e.range.getColumn() > usageCol || e.range.getLastColumn() < usageCol) return;

    const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
    if (!tok) {
      // Do NOT fail silently. Without the token this whole feature does
      // nothing, looks exactly like a broken sheet, and gives no clue why —
      // which is how it sat unnoticed. One email, at most once a day, then
      // back to quiet.
      const props = PropertiesService.getScriptProperties();
      const today = Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'yyyy-MM-dd');
      if (props.getProperty('TOKEN_WARNED_ON') !== today) {
        props.setProperty('TOKEN_WARNED_ON', today);
        alert_('חסר ORDERS_TOKEN — מד הניצול לא מתעדכן',
          'ערכת את עמודת הניצול בטבלת הקבלות, אבל הסנכרון המיידי לאתר לא רץ ' +
          'כי אין ORDERS_TOKEN במאפייני הסקריפט.\n\n' +
          'תיקון: עורך הסקריפט → ⚙️ הגדרות הפרויקט → מאפייני סקריפט → ' +
          'הוספת מאפיין → שם: ORDERS_TOKEN → הדבק את הערך → שמירה.\n' +
          'אחר כך הרץ checkReceiptsSync כדי לוודא שהכול עובד.');
      }
      return;
    }

    const items = [];
    for (let r = Math.max(2, e.range.getRow()); r <= e.range.getLastRow(); r++) {
      const orderId = String(sh.getRange(r, orderCol).getValue()).trim();
      // The cell reads "used / total", e.g. "0.44 / 1".
      const m = String(sh.getRange(r, usageCol).getValue()).match(/([\d.]+)\s*\/\s*([\d.]+)/);
      if (!orderId || !m) continue;
      const used = parseFloat(m[1]), total = parseFloat(m[2]);
      if (!(total > 0) || !(used >= 0)) continue;
      items.push({ order_id: orderId, used_gb: used, total_gb: total });
    }
    if (!items.length) return;

    const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + tok },
      payload: JSON.stringify({ action: 'usage_batch', items: items }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() !== 200) {
      Logger.log('usage push failed: ' + res.getContentText().slice(0, 200));
    }
  } catch (err) {
    Logger.log('onReceiptsEdit failed: ' + err);
  }
}

/**
 * Why is the usage meter not updating? Run this and read the log.
 *
 * Every part of this chain fails quietly by design (a sync problem must never
 * block someone editing a sheet), so when it does not work there is nothing
 * to see anywhere. This checks each link in order and says which one is broken
 * in plain words — instead of leaving "it just doesn't update" to guesswork.
 */
function checkReceiptsSync() {
  const out = [];
  const ok = (s) => out.push('✅ ' + s);
  const bad = (s) => out.push('❌ ' + s);

  const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
  if (tok) ok('ORDERS_TOKEN קיים במאפייני הסקריפט (' + tok.length + ' תווים)');
  else bad('חסר ORDERS_TOKEN → הגדרות הפרויקט ⚙️ → מאפייני סקריפט → ' +
           'שם: ORDERS_TOKEN, ערך: הטוקן של האתר');

  const trig = ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'onReceiptsEdit');
  if (trig.length) ok('הטריגר onReceiptsEdit מותקן (' + trig.length + ')');
  else bad('הטריגר onReceiptsEdit לא מותקן → הרץ setupTriggers מהתפריט');

  let sh = null;
  try {
    sh = SpreadsheetApp.openById(RECEIPTS_ID).getSheets()[0];
    ok('טבלת הקבלות נפתחת: "' + sh.getName() + '"');
  } catch (e) {
    bad('אין גישה לטבלת הקבלות: ' + e);
  }

  let sample = null;
  if (sh) {
    const hdr = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].map(h => String(h).trim());
    const uc = hdr.indexOf(RCPT_USAGE_COL) + 1, oc = hdr.indexOf(RCPT_ORDER_COL) + 1;
    if (uc) ok('עמודת הניצול "' + RCPT_USAGE_COL + '" נמצאה (עמודה ' + uc + ')');
    else bad('לא נמצאה עמודה בשם "' + RCPT_USAGE_COL + '" — שינוי שם הכותרת מנתק את הסנכרון');
    if (oc) ok('עמודת מספר ההזמנה נמצאה (עמודה ' + oc + ')');
    else bad('לא נמצאה עמודה בשם "' + RCPT_ORDER_COL + '"');

    if (uc && oc) {
      const last = sh.getLastRow();
      for (let r = 2; r <= last; r++) {
        const id = String(sh.getRange(r, oc).getValue()).trim();
        const m = String(sh.getRange(r, uc).getValue()).match(/([\d.]+)\s*\/\s*([\d.]+)/);
        if (id && m) { sample = { row: r, id: id, used: parseFloat(m[1]), total: parseFloat(m[2]) }; break; }
      }
      if (sample) ok('שורה לדוגמה: ' + sample.id + ' = ' + sample.used + '/' + sample.total + ' GB (שורה ' + sample.row + ')');
      else out.push('ℹ️ אין עדיין שורה עם ניצול בפורמט "0.4 / 1" — לכן אין מה לשלוח');
    }
  }

  // The real proof: send that reading to the site now and report the answer.
  if (tok && sample) {
    const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders', {
      method: 'post', contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + tok },
      payload: JSON.stringify({ action: 'usage_batch', items: [
        { order_id: sample.id, used_gb: sample.used, total_gb: sample.total }] }),
      muteHttpExceptions: true,
    });
    const code = res.getResponseCode(), txt = res.getContentText();
    if (code === 200) {
      const body = JSON.parse(txt || '{}');
      if ((body.updated || []).length) ok('האתר עודכן בהצלחה עבור ' + sample.id + ' — הסנכרון עובד מקצה לקצה');
      else if ((body.not_found || []).length) bad('האתר לא מכיר את ההזמנה ' + sample.id + ' (ייתכן שנמחקה או ישנה מ-90 יום)');
      else out.push('ℹ️ האתר ענה 200 בלי לעדכן: ' + txt.slice(0, 200));
    } else if (code === 401 || code === 403) {
      bad('האתר דחה את הטוקן (' + code + ') — ה-ORDERS_TOKEN כאן שונה מזה שבאתר');
    } else {
      bad('האתר החזיר ' + code + ': ' + txt.slice(0, 200));
    }
  }

  const text = out.join('\n');
  Logger.log(text);
  return text;
}

function fullSync() { post_(buildPackages_(null)); }

function previewLog() {
  const pkgs = buildPackages_(null);
  Logger.log('packages: ' + pkgs.length);
  Logger.log(JSON.stringify({ packages: pkgs }, null, 2));
}

function runScrapeNow() {
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) throw new Error('חסר GH_TOKEN ב-Script Properties (הגדרות הפרויקט)');
  const res = UrlFetchApp.fetch(GH_DISPATCH, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });
  const ok = res.getResponseCode() === 204;
  if (!ok) alert_('הפעלת הסקרייפר נכשלה', res.getContentText().slice(0, 500));
  Logger.log(ok ? 'הסריקה הופעלה ב-GitHub ✓' : 'שגיאה: ' + res.getContentText());
}

// ── fulfillment bot dispatcher — every 5 minutes ────────────────────
// GitHub throttles scheduled workflows on public repos (observed: */5 cron
// firing ~once an hour). Apps Script triggers are punctual, so this tick
// dispatches the fulfillment workflow instead. Costs ~1s per run — far
// inside the daily trigger quota. Failures alert at most once per 6h.
function fulfillmentTick() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GH_TOKEN');
  if (!token) return;                        // not configured — GitHub cron still runs

  // The trigger fires every MINUTE, but dispatching every minute would mean
  // 1440 Actions runs a day for an inbox that is empty almost all the time.
  // So: dispatch at once while a paid order is still waiting for its eSIM
  // (customer gets the QR in ~1 minute instead of up to 5), and otherwise
  // keep the old 5-minute cadence — same idle cost as before.
  // Finish any order whose eSIM was still being provisioned when the purchase
  // bot handed over its supplier session. Usually a no-op — the site normally
  // completes the order on the spot — but it is what closes the gap when the
  // supplier is a few seconds slow, without waiting for the delivery email.
  sweepProvisioningOrders_();
  // Watch whether the supplier can still sell us packages, and email once on
  // each change. Also keeps the site's cached verdict warm, so a shopper's
  // page load never has to wait for a live check.
  supplierWatch_();
  // Anything an edit queued but could not send (its flush was already busy)
  // goes out here, so a price change can never sit unsent.
  flushPendingRows_();

  if (!orderAwaitingEsim_() && new Date().getMinutes() % 5 !== 0) return;

  try {
    const res = UrlFetchApp.fetch(FULFILL_DISPATCH, {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
      payload: JSON.stringify({ ref: 'main' }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() === 204) return;         // dispatched ✓
    throw new Error('HTTP ' + res.getResponseCode() + ': ' +
      res.getContentText().slice(0, 300));
  } catch (err) {
    const last = +(props.getProperty('FT_LAST_ALERT') || 0);
    if (Date.now() - last > 6 * 36e5) {
      props.setProperty('FT_LAST_ALERT', String(Date.now()));
      alert_('הפעלת בוט המימוש מה-Apps Script נכשלת',
        String(err) + '\n(הבוט עדיין רץ מה-cron של GitHub, רק לאט יותר. ' +
        'התראה זו נשלחת לכל היותר פעם ב-6 שעות.)');
    }
    Logger.log('fulfillmentTick failed: ' + err);
  }
}

// Receipts columns (1-based) the tick reads. The purchase bot appends a row
// the moment it PAYS; the fulfillment bot later fills the activation code in
// from esim.dog's delivery email. A row with an order id and no activation
// code is therefore an order mid-flight.
const RCP_DATE_COL = 2;         // תאריך
const RCP_ORDER_COL = 6;        // מס׳ הזמנה
const RCP_ACTIVATION_COL = 8;   // Activation Code
const AWAITING_WINDOW_MS = 30 * 60 * 1000;

function rowTime_(v) {
  if (v instanceof Date) return v.getTime();
  // The bot writes DD/MM/YYYY HH:MM:SS — day first, so Date.parse would read
  // 07/12 as 7 December in some locales and 12 July in others. Parse it by hand.
  const m = String(v).match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return NaN;
  return new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5], +(m[6] || 0)).getTime();
}

// ── supplier watch — every minute ───────────────────────────────────
// On 2026-07-27 the supplier answered HTTP 200 on every page while all of its
// JavaScript build files 404'd: the site rendered, the Checkout button did
// nothing, and nobody could buy. We could still have taken payments for
// packages we had no way to obtain.
//
// So this asks the site to re-run its real check (page loads AND its build
// files exist), which both keeps the cached verdict warm for shoppers and
// tells us the moment selling becomes impossible — or possible again.
// Emails only on a CHANGE, so a long outage does not send 1440 messages.
function supplierWatch_() {
  const props = PropertiesService.getScriptProperties();
  const tok = props.getProperty('ORDERS_TOKEN');
  if (!tok) return;                          // not configured — site self-checks

  let selling, reason;
  try {
    const res = UrlFetchApp.fetch('https://www.waverole.com/api/supplier-status', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + tok },
      payload: JSON.stringify({ action: 'refresh' }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() !== 200) return;
    const body = JSON.parse(res.getContentText());
    selling = body.selling !== false;
    reason = body.reason || '';
  } catch (e) {
    return;                                  // a watch failure is not an outage
  }

  const was = props.getProperty('SUPPLIER_SELLING');
  const now = selling ? 'yes' : 'no';
  if (was === now) return;                   // nothing changed — stay quiet
  props.setProperty('SUPPLIER_SELLING', now);
  if (was === null) return;                  // first ever run — no news yet

  if (!selling) {
    alert_('הספק לא זמין — המכירות נעצרו אוטומטית',
      'לא ניתן לרכוש חבילות מהספק כרגע, ולכן האתר עבר למצב תחזוקה ואי אפשר לקנות בו.\n\n' +
      'סיבה: ' + reason + '\n\n' +
      'הזמנות קיימות ממשיכות לפעול כרגיל — רק מכירות חדשות מושבתות.\n' +
      'האתר ייפתח מחדש מעצמו תוך כדקה מרגע שהספק יחזור.');
  } else {
    // Buying works again — hand back every order that was paid for but could
    // not be bought while the supplier was down, before saying all is well.
    const rescued = retryUnfulfilled_(tok);
    report_('הספק חזר — המכירות נפתחו מחדש',
      'ניתן שוב לרכוש חבילות מהספק, והאתר חזר לפעולה רגילה.' +
      (rescued ? '\n\nהוחזרו לתור ' + rescued + ' הזמנות ששולמו ולא סופקו בזמן התקלה.' : ''));
  }
}

// Give paid-but-unbought orders back to the bot. Returns how many.
// Orders that have used up their retries are NOT returned here — the site
// emails about those separately, because they need a person.
function retryUnfulfilled_(tok) {
  try {
    const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + tok },
      payload: JSON.stringify({ action: 'retry_unfulfilled' }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() !== 200) return 0;
    return (JSON.parse(res.getContentText()).requeued || []).length;
  } catch (e) {
    return 0;
  }
}

function sweepProvisioningOrders_() {
  const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
  if (!tok) return;                          // optional — see setup notes above
  try {
    const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + tok },
      payload: JSON.stringify({ action: 'sweep' }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() === 200) {
      const done = (JSON.parse(res.getContentText()).fulfilled || []);
      if (done.length) Logger.log('sweep completed: ' + done.join(', '));
    }
  } catch (err) {
    Logger.log('sweep failed: ' + err);      // never break the dispatcher
  }
}

function orderAwaitingEsim_() {
  // Signal 1 — the SITE's own queue: an order sits there as "pending" from
  // the second the payment IPN lands, before the PC bot has done anything.
  // The receipts-row signal below only exists AFTER the PC bot both bought
  // and wrote the row — the night WR-845JFY got stuck proved that row can
  // simply never appear. Optional: needs ORDERS_TOKEN in Script Properties
  // (same value as the site's env var); skipped silently without it.
  try {
    const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
    if (tok) {
      const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders?status=pending', {
        headers: { Authorization: 'Bearer ' + tok },
        muteHttpExceptions: true,
      });
      if (res.getResponseCode() === 200) {
        const orders = JSON.parse(res.getContentText()).orders || [];
        for (const o of orders) {
          const age = Date.now() - new Date(o.ts).getTime();
          if (Math.abs(age) < AWAITING_WINDOW_MS) return true;
        }
      }
    }
  } catch (err) {
    Logger.log('site queue check failed: ' + err);   // fall through to the sheet
  }
  // Signal 2 — a receipts row with an order number and no activation code
  // (order bought, eSIM email not yet processed).
  try {
    const sh = SpreadsheetApp.openById(RECEIPTS_ID).getSheets()[0];
    const last = sh.getLastRow();
    if (last < 2) return false;
    const n = Math.min(15, last - 1);        // newest rows only — enough for any burst
    const rows = sh.getRange(last - n + 1, 1, n, RCP_ACTIVATION_COL).getValues();
    for (const row of rows) {
      if (!String(row[RCP_ORDER_COL - 1] || '').trim()) continue;      // not an order row
      if (String(row[RCP_ACTIVATION_COL - 1] || '').trim()) continue;  // already fulfilled
      // Recent rows only, so one permanently stuck order cannot pin the
      // dispatcher at a run every minute forever. The window is symmetric to
      // absorb any timezone skew between the bot and this script.
      const age = Date.now() - rowTime_(row[RCP_DATE_COL - 1]);
      if (Math.abs(age) < AWAITING_WINDOW_MS) return true;
    }
  } catch (err) {
    // Never let this gate break the dispatcher — fall back to the 5-min cadence.
    Logger.log('orderAwaitingEsim_ failed: ' + err);
  }
  return false;
}

function dailyScrape() {
  // Dispatching the GitHub scraper needs a GH_TOKEN. Without one this step
  // is SKIPPED SILENTLY — the scraper has its own daily schedule on GitHub,
  // so no alert is needed (it used to email an error every morning).
  const gh = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (gh) {
    try {
      runScrapeNow();
    } catch (err) {
      alert_('dailyScrape נכשל', String(err));
    }
  } else {
    Logger.log('GH_TOKEN not set — skipping dispatch (GitHub cron handles the scrape).');
  }
  // Full site sync 45 min later — after the scraper wrote fresh data to the
  // sheet. Programmatic writes don't fire onEdit, so this sync is the ONLY
  // path that gets the daily price changes to the site.
  ScriptApp.newTrigger('fullSyncOnce').timeBased().after(45 * 60 * 1000).create();
}

function fullSyncOnce() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'fullSyncOnce')
    .forEach(t => ScriptApp.deleteTrigger(t));
  try {
    fullSync();
  } catch (err) {
    alert_('הסנכרון היומי המלא נכשל', String(err));
  }
}

// ── weekly Drive backup of both spreadsheets ────────────────────────
function weeklyBackup() {
  try {
    const it = DriveApp.getFoldersByName(BACKUP_FOLDER);
    const folder = it.hasNext() ? it.next() : DriveApp.createFolder(BACKUP_FOLDER);
    const stamp = Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'yyyy-MM-dd');
    [SHEET_ID, RECEIPTS_ID].forEach(function (id) {
      const src = DriveApp.getFileById(id);
      const base = src.getName().replace(/ \(backup .*\)$/, '');
      src.makeCopy(base + ' (backup ' + stamp + ')', folder);
      // Prune: keep only the newest BACKUP_KEEP copies of this spreadsheet.
      const copies = [];
      const files = folder.getFiles();
      while (files.hasNext()) {
        const f = files.next();
        if (f.getName().indexOf(base + ' (backup ') === 0) copies.push(f);
      }
      copies.sort(function (a, b) { return b.getDateCreated() - a.getDateCreated(); });
      copies.slice(BACKUP_KEEP).forEach(function (f) { f.setTrashed(true); });
    });
    Logger.log('weekly backup done → Drive folder "' + BACKUP_FOLDER + '"');
  } catch (err) {
    alert_('הגיבוי השבועי של הטבלאות נכשל', String(err));
  }
}

// Manual test: verifies the alert-email path works (run from the editor).
function testAlert() {
  alert_('בדיקת מערכת ההתראות',
    'אם קיבלת את המייל הזה — מערכת ההתראות של סנכרון המחירים עובדת ✓');
  Logger.log('test alert sent to ' + ALERT_EMAIL);
}

// ── watchdog: is the live site actually fresh? ──────────────────────
// The handlers that must be installed for the automation to exist at all.
// Kept next to the watchdog rather than inside setupTriggers so that adding a
// feature here forces the question "and is it actually running?".
const EXPECTED_TRIGGERS = ['onEditPush', 'onReceiptsEdit', 'dailyScrape',
                           'checkSiteFresh', 'fulfillmentTick', 'weeklyBackup'];

function checkSiteFresh() {
  // Is the automation even installed?
  //
  // A trigger that was never created fails in the most expensive way there is:
  // in perfect silence. onReceiptsEdit sat missing for days — the code existed,
  // was correct, was tested, and simply had never been deployed, so editing the
  // receipts sheet did nothing and there was nothing anywhere to say why. Newly
  // written code that is never installed looks exactly like broken code.
  try {
    const installed = ScriptApp.getProjectTriggers().map(t => t.getHandlerFunction());
    const absent = EXPECTED_TRIGGERS.filter(f => installed.indexOf(f) < 0);
    if (absent.length) {
      alert_('טריגרים חסרים — חלק מהאוטומציה לא רצה בכלל',
        'הטריגרים האלה לא מותקנים: ' + absent.join(', ') + '\n\n' +
        'כל עוד הם חסרים הם פשוט לא קורים, בלי שום הודעת שגיאה.\n' +
        'תיקון: עורך הסקריפט → בחר setupTriggers בתפריט הפונקציות → הרץ ▶');
    }
  } catch (err) {
    Logger.log('trigger check failed: ' + err);
  }

  // Upstream first: if the SCRAPER stopped writing, the sheet quietly ages,
  // every sync "succeeds" with stale numbers, and the purchase bot compares
  // esim.dog against yesterday's prices. Last-modified of the price sheet is
  // a good liveness proxy (the daily scrape rewrites it every morning).
  try {
    const modified = DriveApp.getFileById(SHEET_ID).getLastUpdated();
    const sheetAgeH = (Date.now() - modified.getTime()) / 36e5;
    if (sheetAgeH > MAX_STALE_HOURS) {
      alert_('טבלת המחירים עצמה לא התעדכנה ' + Math.round(sheetAgeH) + ' שעות',
        'העדכון האחרון של הקובץ: ' + modified.toISOString() +
        '\nכנראה שהסקרייפר היומי (GitHub Actions) לא רץ או נכשל — ' +
        'בדוק את esim-price-scraper → Actions → price scrape.' +
        '\nעד שיתוקן, הבוטים עובדים לפי מחירים ישנים.');
    }
  } catch (err) {
    Logger.log('sheet-freshness check failed: ' + err);
  }
  try {
    const res = UrlFetchApp.fetch(OVERLAY_URL + '?cb=' + Date.now(),
      { muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) {
      alert_('watchdog: האתר לא מחזיר את קובץ הנתונים',
        'HTTP ' + res.getResponseCode() + ' מ-' + OVERLAY_URL);
      return;
    }
    const updated = new Date(JSON.parse(res.getContentText()).updated);
    const hours = (Date.now() - updated.getTime()) / 36e5;
    const ageStr = hours.toFixed(1) + ' שעות';
    Logger.log('site data age: ' + hours.toFixed(1) + 'h');
    // The overlay `updated` only moves when a price actually CHANGED (the
    // endpoint skips no-op commits). A successful recent sync is just as
    // fresh — the site provably has today's numbers, they're identical.
    const lastOk = PropertiesService.getScriptProperties().getProperty('LAST_SYNC_OK');
    const okHours = lastOk ? (Date.now() - new Date(lastOk).getTime()) / 36e5 : Infinity;
    if (!(hours < MAX_STALE_HOURS) && !(okHours < MAX_STALE_HOURS)) {
      alert_('הנתונים באתר לא התעדכנו ' + Math.round(hours) + ' שעות',
        'העדכון האחרון באתר: ' + updated.toISOString() +
        '\nוגם לא היה סנכרון מוצלח ב-' + MAX_STALE_HOURS + ' השעות האחרונות.' +
        '\nכנראה שהסנכרון היומי לא רץ או נכשל.' +
        '\nלתיקון מיידי: להריץ fullSync מעורך ה-Apps Script.');
    } else if (!(hours < MAX_STALE_HOURS)) {
      report_('הבדיקה היומית עברה — האתר מעודכן ✓',
        'הסנכרון האחרון רץ בהצלחה לפני ' + okHours.toFixed(1) + ' שעות ולא מצא ' +
        'שינויי מחירים (ולכן חותמת האתר לא זזה — זה תקין).' +
        '\nחותמת נתוני האתר: ' + updated.toISOString());
    } else {
      // Daily all-clear so a quiet inbox is proof it ran, not that it broke.
      report_('הבדיקה היומית עברה — האתר מעודכן ✓',
        'הנתונים באתר עודכנו לפני ' + ageStr + ' (הכל תקין).' +
        '\nעדכון אחרון באתר: ' + updated.toISOString());
    }
  } catch (err) {
    alert_('watchdog נכשל', String(err));
  }
}
