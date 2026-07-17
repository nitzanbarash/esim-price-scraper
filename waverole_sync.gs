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
 *       SITE_TOKEN = the UPDATE_PACKAGES_TOKEN value (site's .env.local)
 *       GH_TOKEN   = GitHub PAT (repo+workflow) for esim-price-scraper
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
  ScriptApp.newTrigger('dailyScrape').timeBased()
    .atHour(10).everyDays(1).inTimezone('Asia/Jerusalem').create();
  ScriptApp.newTrigger('checkSiteFresh').timeBased()
    .atHour(12).everyDays(1).inTimezone('Asia/Jerusalem').create();
  // GitHub throttles */5 cron on public repos to ~1/hour in practice, so the
  // fulfillment bot is dispatched from here instead — Apps Script's 5-minute
  // trigger actually fires every 5 minutes. Needs GH_TOKEN (skips without it).
  ScriptApp.newTrigger('fulfillmentTick').timeBased().everyMinutes(5).create();
  // Weekly Drive copies of both spreadsheets — the sheets ARE the business
  // (prices, receipts, eSIM codes); an accidental mass-delete or a broken
  // formula paste would otherwise be unrecoverable beyond version history.
  ScriptApp.newTrigger('weeklyBackup').timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY).atHour(3).inTimezone('Asia/Jerusalem').create();
  Logger.log('Triggers installed: onEdit sync + daily 10:00 scrape + 12:00 watchdog + 5-min fulfillment tick + weekly backup');
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
  const token = PropertiesService.getScriptProperties().getProperty('SITE_TOKEN');
  if (!token) throw new Error('חסר SITE_TOKEN ב-Script Properties (הגדרות הפרויקט)');
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
    const rows = new Set();
    for (let r = Math.max(2, e.range.getRow()); r <= e.range.getLastRow(); r++) rows.add(r);
    if (!rows.size) return;
    const pkgs = buildPackages_(rows);
    if (pkgs.length) post_(pkgs);
  } catch (err) {
    // colMap_/post_ already emailed the specific reason; log and stop.
    Logger.log('onEditPush failed: ' + err);
  }
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
function checkSiteFresh() {
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
