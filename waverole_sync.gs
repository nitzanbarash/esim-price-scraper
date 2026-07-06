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
 *     (installable onEdit trigger).
 *  2. Daily 10:00 Israel: starts the GitHub scraper, then a full site sync
 *     45 minutes later (after the scrape finished writing fresh data).
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
 *   previewLog   — log the exact JSON that would be sent (dry run)
 *   fullSync     — push all packages to the site now
 *   runScrapeNow — trigger the GitHub scraper now
 */

const ENDPOINT = 'https://www.waverole.com/api/update-packages';
const GH_DISPATCH = 'https://api.github.com/repos/nitzanbarash/esim-price-scraper/actions/workflows/scrape.yml/dispatches';
const SHEET_ID = '108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo';

// Row-1 header text (trimmed) → API field
const HEADERS = {
  sku:         'חבילה (קוד)',
  gb:          'GB',
  days:        'זמן חבילה',
  networks:    'Networks',
  breakout_ip: 'Breakout IP',
  stock:       'במלאי/רווחי',       // empty = in stock; "לא במלאי"/"לא רווחי" = out
  fee:         'סליקה',
  price:       'כולל מעמ',          // FINAL customer price (incl. VAT + fee)
  sale:        'מבעצעים (אחוזים)',  // empty/0 cancels the sale
};

function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('onEditPush')
    .forSpreadsheet(SHEET_ID).onEdit().create();
  ScriptApp.newTrigger('dailyScrape').timeBased()
    .atHour(10).everyDays(1).inTimezone('Asia/Jerusalem').create();
  Logger.log('Triggers installed: onEdit sync + daily 10:00 scrape');
}

// ── helpers ─────────────────────────────────────────────────────────
function sheet_() {
  return SpreadsheetApp.openById(SHEET_ID).getSheets()[0];
}

function colMap_(sheet) {
  const head = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const map = {};
  for (const [key, name] of Object.entries(HEADERS)) {
    const i = head.findIndex(h => String(h).trim() === name);
    if (i >= 0) map[key] = i;                       // 0-based
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
  let msg = 'HTTP ' + code;
  try {
    const j = JSON.parse(body);
    msg = 'עודכנו ' + (j.updated || []).length +
      ((j.not_found || []).length ? ' | לא נמצאו: ' + j.not_found.join(', ') : '') +
      ((j.warnings || []).length ? ' | ⚠️ ' + j.warnings.length + ' אזהרות' : '');
  } catch (err) {}
  Logger.log(msg);
  try { SpreadsheetApp.openById(SHEET_ID).toast(msg, 'Waverole', 8); } catch (e) {}
  return body;
}

// ── actions ─────────────────────────────────────────────────────────
function onEditPush(e) {
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
  Logger.log(ok ? 'הסריקה הופעלה ב-GitHub ✓' : 'שגיאה: ' + res.getContentText());
}

function dailyScrape() {
  runScrapeNow();
  // Full site sync 45 min later — after the scraper wrote fresh data to the sheet
  ScriptApp.newTrigger('fullSyncOnce').timeBased().after(45 * 60 * 1000).create();
}

function fullSyncOnce() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'fullSyncOnce')
    .forEach(t => ScriptApp.deleteTrigger(t));
  fullSync();
}
