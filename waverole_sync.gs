/**
 * Waverole <-> Google Sheet sync - STANDALONE Apps Script project.
 *
 * Why standalone: the spreadsheet sits in shared storage whose security
 * restrictions block creating a container-bound script (Drive refuses it as a
 * security restriction).
 * A standalone project + installable triggers works around that: it opens
 * the sheet by ID, so no binding is needed. Limitation: standalone scripts
 * cannot add a custom menu inside the sheet - manual actions run from the
 * Apps Script editor (Run >) instead.
 *
 * What it does:
 *  1. INSTANT site update whenever a relevant cell is edited in the sheet
 *     (installable onEdit trigger). NOTE: programmatic writes (the daily
 *     scraper) do NOT fire onEdit - that's what the daily full sync is for.
 *  2. Daily 10:00 Israel: starts the GitHub scraper, then a full site sync
 *     45 minutes later (after the scrape finished writing fresh data).
 *  3. Daily 12:00 Israel: WATCHDOG - verifies the live site data is fresh;
 *     emails ALERT_EMAIL if the site wasn't updated in the last 26 hours.
 *  4. Any failure (missing column, HTTP error, exception) emails ALERT_EMAIL
 *     instead of failing silently.
 *
 * One-time setup (in the Apps Script editor, script.google.com):
 *  1. Paste this file over Code.gs -> Save (Cmd+S).
 *  2. Project Settings ((settings)) -> Script properties -> add:
 *       UPDATE_PACKAGES_TOKEN = the site's UPDATE_PACKAGES_TOKEN
 *                      (was called SITE_TOKEN here; both names still work)
 *       GH_TOKEN     = GitHub PAT (repo+workflow) for esim-price-scraper
 *       ORDERS_TOKEN = the site's ORDERS_TOKEN - the SAME value the PC bot
 *                      and GitHub Actions already use. There is only ever
 *                      ONE of these: the site checks one string, so every
 *                      client presents that same string. Never mint a second
 *                      one - it is also the HMAC key that signs the payment
 *                      callback, so a mismatch silently rejects real orders.
 *                      Optional here; without it the 1-min fulfillment tick
 *                      cannot see a paid order until the PC bot reports it.
 *  3. In the editor pick `setupTriggers` in the function dropdown -> Run >
 *     -> authorize when prompted. Done.
 *
 * Install / re-install, every time (the short version):
 *     paste over Code.gs -> run setupTriggers -> authorize
 *
 * AFTER EVERY PASTE, RUN setupTriggers ONCE. Pasting only replaces the
 * code; the triggers are separate objects and an older paste's triggers
 * keep firing the old handlers (or none at all, if a handler was renamed).
 * setupTriggers deletes every trigger this project owns and installs the
 * current set, so running it is what makes the paste take effect.
 *
 * Manual actions (function dropdown -> Run >):
 *   previewLog     - log the exact JSON that would be sent (dry run)
 *   fullSync       - push all packages to the site now
 *   runScrapeNow   - trigger the GitHub scraper now
 *   checkSiteFresh - run the freshness watchdog now
 *   syncCouponsNow - push the coupon tab and pull the use counters now
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

// Row-1 header text (trimmed) -> API field.
// Each field lists EVERY name the column has ever had, so renaming a header
// doesn't silently break the sync again (2026-07-09: the incl.-VAT header was
// renamed to the final-price one and it went unnoticed, so price updates
// stopped reaching the site).
const HEADERS = {
  sku:         ['\u05d7\u05d1\u05d9\u05dc\u05d4 (\u05e7\u05d5\u05d3)'],
  gb:          ['GB'],
  days:        ['\u05d6\u05de\u05df \u05d7\u05d1\u05d9\u05dc\u05d4'],
  networks:    ['Networks'],
  breakout_ip: ['Breakout IP'],
  source:      ['\u05de\u05e7\u05d5\u05e8'],                     // which supplier this row prices
  chosen:      ['\u05e0\u05d1\u05d7\u05e8'],                     // (tick) = the row of the pair the site sells
  stock:       ['\u05d1\u05de\u05dc\u05d0\u05d9/\u05e8\u05d5\u05d5\u05d7\u05d9'],              // empty = in stock
  fee:         ['\u05e1\u05dc\u05d9\u05e7\u05d4'],                     // what the CUSTOMER is shown (the ladder)
  my_price:    ['\u05de\u05d7\u05d9\u05e8 \u05e9\u05dc\u05d9'],                // what actually lands, after the real cut
  price:       ['\u05de\u05d7\u05d9\u05e8 \u05e1\u05d5\u05e4\u05d9', '\u05db\u05d5\u05dc\u05dc \u05de\u05e2\u05de'],    // FINAL customer price (incl. VAT + fee)
  sale:        ['\u05de\u05d1\u05e2\u05e6\u05e2\u05d9\u05dd (\u05d0\u05d7\u05d5\u05d6\u05d9\u05dd)'],         // empty/0 cancels the sale
  buy:         ['\u05de\u05d7\u05d9\u05e8 \u05e7\u05e0\u05d9\u05d9\u05d4'],               // what the SUPPLIER charges us (scraper writes it)
  profit:      ['\u05e8\u05d5\u05d5\u05d7 (\u05db\u05d3\u05d0\u05d9\u05d5\u05ea)'],           // derived: net minus buy, in $ and %
};
// Fields the sync cannot work without - missing => loud email, not silence.
const REQUIRED_FIELDS = ['sku', 'price'];

// The colours the owner already paints by hand (read off the live sheet
// 2026-09-10): green = the row the site sells, grey = the other supplier's row.
const ROW_CHOSEN_BG   = '#d8efd3';
const ROW_UNCHOSEN_BG = '#f2f2f2';

// -- the two fees, and why there are two -----------------------------
//
// The sheet's own table (top right, "\u05d8\u05d5\u05d5\u05d7 \u05de\u05d7\u05d9\u05e8"/"\u05e2\u05de\u05dc\u05d4") is a PRICING ladder,
// not a cost. It is what the customer is shown and what the site displays:
// $0.40 up to $2, $0.60 up to $5, and so on. Above $20 it is zero - the fee
// is on us, so the customer sees none.
//
// What the processor actually keeps is a different number: 4% of the sale
// plus $0.35. That one is nobody's business but the owner's, so it never
// leaves the sheet - it is subtracted in '\u05de\u05d7\u05d9\u05e8 \u05e9\u05dc\u05d9', the private column.
//
// Before 2026-09-10 both numbers lived in one column and 39 of 84 rows had
// drifted off the ladder, some by more than a third of the fee. Typing the
// final price is now the ONLY input; the other two are derived, so they
// cannot disagree again.
const FEE_LADDER    = [[2, 0.4], [5, 0.6], [10, 0.8], [15, 1.0], [20, 1.2]];
const FEE_OVER_TOP  = 0;      // over $20: shown as no fee, absorbed by us
const REAL_FEE_RATE = 0.04;   // 4% of the sale ...
const REAL_FEE_FIXED = 0.35;  // ... plus 35 cents

function tableFee_(price) {
  for (let i = 0; i < FEE_LADDER.length; i++) {
    if (price <= FEE_LADDER[i][0]) return FEE_LADDER[i][1];
  }
  return FEE_OVER_TOP;
}

function realFee_(price) {
  return Math.round((price * REAL_FEE_RATE + REAL_FEE_FIXED) * 100) / 100;
}

// -- profitability, the same arithmetic the scraper does --------------
//
// The scraper writes this pair (profit text + the unprofitable marker) every
// morning off the fresh buy price. When the OWNER types a new sell price the
// buy price has not moved, but the profit has - so the same two cells have to
// follow the edit, or the sheet shows a profit computed against a price that
// is no longer on the row. Mirrors esim_price_scraper.py's profitability
// check, floor included.
const PROFIT_MIN_PCT     = 20;    // a package has to clear 20% ...
const PROFIT_MIN_PCT_1GB = -20;   // ... except 1GB, the loss leader
const UNPROFITABLE = '\u05dc\u05d0 \u05e8\u05d5\u05d5\u05d7\u05d9';

function profitFloorPct_(gb) {
  return (gb !== null && gb <= 1) ? PROFIT_MIN_PCT_1GB : PROFIT_MIN_PCT;
}

// The buy-price cell is written by the scraper as text and can carry more
// than one figure (a shekel figure beside the dollar one, a euro one on a
// Stellar row). The dollar amount is the one we paid, so it is read by its
// '$' and never by position.
//
// PARENTHESES, not position, and that is the point. Until 2026-09-10 the pair
// was written dollars-first, '$0.56 (\u20ac0.48)', and this helper was ANCHORED
// so that a cell whose leading amount was not dollars could never yield one.
// The owner has now asked for the euro first, '(\u20ac0.48) $0.56', and the
// sheet holds BOTH spellings until every row has been rewritten - so an
// anchored read would be right about half the sheet and blind to the rest,
// and a blind row loses its profit figure and its unprofitable marker.
//
// So the rule moved from the start of the cell to the brackets. The dollars
// OUTSIDE them are what we paid; a '$' INSIDE them is a converted estimate of
// some other currency ('\u20ac0.48 ($0.56)'), and computing profit against
// that prices the row on money nobody spent. Every (...) group is dropped
// first - innermost outwards, so nesting unwinds too - and only then is a '$'
// amount looked for. Nothing is anchored any more, which is also how the
// invisible bidi marks Sheets sprinkles into RTL text stopped mattering.
//
// A bare number is NOT a price, so "12.90 NIS" can never pass as $12.90 and a
// naked "0.56" is read as nothing at all - the same rule, and now the same
// shape, as choose_supplier.py's usd_outside_parens().
const PARENS_ = /\([^()]*\)/g;
const USD_RE_ = /\$\s*(\d[\d,]*(?:\.\d+)?)/;

function firstDollar_(v) {
  let s = String(v === null || v === undefined ? '' : v);
  for (let prev = null; prev !== s; ) { prev = s; s = s.replace(PARENS_, ' '); }
  const m = USD_RE_.exec(s);
  if (!m) return null;
  const n = parseFloat(m[1].replace(/,/g, ''));
  return isNaN(n) ? null : n;
}

// Runnable from the Apps Script editor (Run > checkPriceReading), because
// nothing else here can be: this file has no test runner, and the one rule it
// shares with three Python bots is the rule that decides what a package cost.
// The cases are the same six pinned in test_choose_supplier.py.
function checkPriceReading() {
  const cases = [
    ['$5.30 (\u20ac4.55)', 5.30],   // yesterday's order, still in the sheet
    ['(\u20ac4.55) $5.30', 5.30],   // today's order
    ['$1.65', 1.65],                // an esim.dog row: one currency
    ['\u20ac0.48 ($0.56)', null],   // dollars in brackets are an estimate
    ['0.56', null],                 // a bare number is not a price
    ['', null], ['\u2014', null], ['\u05dc\u05d0 \u05d1\u05de\u05dc\u05d0\u05d9', null],
    ['\u200f(\u20ac4.55)\u200e $5.30', 5.30]   // with the bidi marks Sheets adds
  ];
  const bad = cases.filter(function (c) { return firstDollar_(c[0]) !== c[1]; });
  Logger.log(bad.length === 0 ? 'firstDollar_: all ' + cases.length + ' cases pass'
    : 'firstDollar_ FAILED on ' + JSON.stringify(bad.map(function (c) {
        return { cell: c[0], want: c[1], got: firstDollar_(c[0]) };
      })));
}

// The leading emoji is not decoration: it stops Sheets parsing "+$0.03 (..."
// as a formula, and it is how the owner reads the column at a glance.
function profitText_(net, buy) {
  const abs = net - buy;
  const pct = (abs / buy) * 100;
  return {
    pct: pct,
    text: (abs >= 0 ? '\ud83d\udfe2 +' : '\ud83d\udd34 -') + '$' + Math.abs(abs).toFixed(2) +
          ' (' + (pct >= 0 ? '+' : '-') + Math.abs(pct).toFixed(1) + '%)'
  };
}

// One setValues per group of neighbouring cells, instead of one call per cell.
//
// The catch is column R, '<---- ' + do-not-touch: it sits between Q and S, so
// the derived cells fall into TWO blocks, P..Q and S..V, and they are written
// by two separate calls on purpose. A single block from P to V would cover R
// and overwrite it on every edit - value-preserving or not, that column is
// spoken for.
//
// Inside a block, a cell we are not changing is written back with the value
// already in it; that write-back is what lets four cells go out in one call.
// The block is skipped entirely when nothing in it actually differs.
//
// The contiguity check is the safety rail: if a group's own columns ever stop
// being adjacent (a header moved, a column was inserted), the span would cover
// somebody else's column, so that case drops back to one write per changed
// cell rather than writing back a cell we do not own - which would destroy a
// formula living there.
function writeGroup_(sheet, row, dataRow, cols, want) {
  const owned = cols.filter(function (c) { return c !== undefined; });
  if (!owned.length) return;
  const differs = function (c) {
    return Object.prototype.hasOwnProperty.call(want, c) && dataRow[c] !== want[c];
  };
  if (!owned.some(differs)) return;                    // nothing to write
  const lo = Math.min.apply(null, owned);
  const hi = Math.max.apply(null, owned);
  if (hi - lo + 1 !== owned.length) {                  // not adjacent - per cell
    owned.forEach(function (c) {
      if (differs(c)) sheet.getRange(row, c + 1).setValue(want[c]);
    });
    return;
  }
  const out = [];
  for (let c = lo; c <= hi; c++) {
    out.push(Object.prototype.hasOwnProperty.call(want, c) ? want[c] : dataRow[c]);
  }
  sheet.getRange(row, lo + 1, 1, out.length).setValues([out]);
}

// Final price typed -> ladder fee, real net, profit and the twin rows follow.
//
// Runs on any edit that touches the final-price column OR the sale-percentage
// column, one row or a pasted block. Clearing the price clears every derived
// cell: a row with no sell price is not for sale, and a stale net beside an
// empty price reads as one. Programmatic writes do not fire onEdit, so writing
// these cells here cannot re-enter.
//
// TWINS: one SKU can occupy several rows - one per supplier - but there is
// only ONE customer price for a package; which supplier we buy from is our
// business and never changes what the buyer pays. So the typed price, its
// sale percentage and the two derived fee cells are copied to every row that
// carries the same SKU. Before this, pricing a package meant typing the same
// number twice, and a tick moved to the other supplier's row could publish
// the price the owner had NOT updated.
//
// A SALE-ONLY edit counts. V is a column the owner edits on its own all the
// time - a discount goes on, a discount comes off, the price itself does not
// move - and while this function only watched U, that edit reached the twin
// rows never. The site then sold the same package at two different discounts
// depending on which supplier row carried the tick. So V alone fires it too,
// and propagates V alone: with U untouched the fee, the net and the profit
// are all still correct, and recomputing them would only invite a rounding
// difference against what the scraper wrote this morning.
//
// The whole sheet is read once (A .. last mapped column) rather than the
// edited block alone, because a twin can sit anywhere; the writes are per row
// and per block, and only where a value actually changes.
//
// Returns the sheet rows this function reached - every row carrying one of
// the edited SKUs, the edited rows included. onEditPush pushes that set on
// top of e.range, because the row whose cells just changed need not be the
// row the site sells: edit the price on the unticked supplier's row and it
// is the TICKED twin, outside e.range entirely, that now has a new price to
// publish. Without this the sheet was right and the shop was a day behind.
function applyFee_(sheet, map, e) {
  const reached = [];
  if (map.price === undefined) return reached;
  const c1 = e.range.getColumn(), c2 = e.range.getLastColumn();
  const hits = function (idx) { return idx !== undefined && idx + 1 >= c1 && idx + 1 <= c2; };
  const hitU = hits(map.price);          // the final price was typed
  const hitV = hits(map.sale);           // the sale percentage was typed
  if (!hitU && !hitV) return reached;
  const first = Math.max(2, e.range.getRow());
  const last  = e.range.getLastRow();
  if (last < first) return reached;
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return reached;
  const width = Math.max.apply(null, Object.values(map)) + 1;
  const data  = sheet.getRange(2, 1, lastRow - 1, width).getValues();   // one read

  // What was just typed, per SKU. With no SKU column there are no twins to
  // find, so each edited row answers only for itself.
  const keyOf = function (i, row) {
    return map.sku === undefined ? '#' + row : String(data[i][map.sku] || '').trim();
  };
  const typed = {};
  for (let row = first; row <= Math.min(last, lastRow); row++) {
    const i = row - 2;
    const key = keyOf(i, row);
    if (!key) continue;                       // spacer row, not a package
    typed[key] = {
      price: data[i][map.price],
      sale:  map.sale === undefined ? null : data[i][map.sale]
    };
  }

  for (let i = 0; i < data.length; i++) {
    const row = i + 2;
    const key = keyOf(i, row);
    if (!key || !typed.hasOwnProperty(key)) continue;
    const src = typed[key];
    reached.push(row);          // every row of an edited SKU, written or not

    // Sale-only edit: carry V across the twins and stop. Nothing else on the
    // row was derived from V, so nothing else on the row is stale.
    if (!hitU) {
      if (map.sale === undefined || map.sku === undefined) continue;
      const wantV = {};
      wantV[map.sale] = src.sale;
      writeGroup_(sheet, row, data[i], [map.sale], wantV);
      continue;
    }

    const price = num_(src.price);
    const net = price === null ? null
              : Math.round((price - realFee_(price)) * 100) / 100;

    // S..V, one call. U is the customer price, verbatim as typed - copied,
    // never re-formatted: these are text cells and re-writing one as a number
    // changes what the sheet renders and what every reader parses back out.
    // V, the sale percentage, belongs to the package and not to the supplier.
    // T is the ladder fee the customer is shown, S what actually lands after
    // the processor's real cut.
    const wantA = {};
    if (map.sku !== undefined) {
      wantA[map.price] = src.price;
      if (map.sale !== undefined) wantA[map.sale] = src.sale;
    }
    if (map.fee !== undefined)      wantA[map.fee] = price === null ? '' : tableFee_(price);
    if (map.my_price !== undefined) wantA[map.my_price] = net === null ? '' : net;
    writeGroup_(sheet, row, data[i],
                [map.my_price, map.fee, map.price, map.sale], wantA);

    // P / Q, one call. Profit against THIS row's buy price: each supplier row
    // keeps its own, because the two rows cost different money at the same
    // sell price, and that difference is the whole point of the comparison.
    const buy = map.buy === undefined ? null : firstDollar_(data[i][map.buy]);
    const judged = !!net && !!buy;
    const wantB = {};
    if (map.profit !== undefined) {
      wantB[map.profit] = judged ? profitText_(net, buy).text : '';
    }
    if (map.stock !== undefined) {
      const gb = num_(data[i][map.gb]);        // '5gb' -> 5
      const bad = judged && profitText_(net, buy).pct < profitFloorPct_(gb);
      const now = String(data[i][map.stock] || '').trim();
      // An unpriced or unquoted row is not called unprofitable - it has not
      // been judged.
      //
      // And this column is SHARED. The scraper parks its own words here
      // (out-of-stock, fewer-days-than-promised, regional-only - all Hebrew,
      // none of them ours) and the owner takes a row off sale by hand, in his
      // own words. Every one of those means the row is already not for sale
      // for a reason more specific than ours, so the marker goes in only
      // where the cell is EMPTY - never over a word somebody else put there.
      // Clearing stays as narrow as it always was: the only word this
      // function erases is the one it wrote itself.
      if (bad) {
        if (now === '') wantB[map.stock] = UNPROFITABLE;
      } else if (now === UNPROFITABLE) {
        wantB[map.stock] = '';
      }
    }
    writeGroup_(sheet, row, data[i], [map.profit, map.stock], wantB);
  }
  SpreadsheetApp.flush();   // the sync below reads these cells back
  return reached;
}

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
  // handler itself decides whether to dispatch - every minute while an order
  // waits for its eSIM, every 5th minute otherwise. Needs GH_TOKEN (skips
  // without it).
  ScriptApp.newTrigger('fulfillmentTick').timeBased().everyMinutes(1).create();
  // Weekly Drive copies of both spreadsheets - the sheets ARE the business
  // (prices, receipts, eSIM codes); an accidental mass-delete or a broken
  // formula paste would otherwise be unrecoverable beyond version history.
  ScriptApp.newTrigger('weeklyBackup').timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY).atHour(3).inTimezone('Asia/Jerusalem').create();
  // A redemption happens on the SITE, so the sheet only learns about it by
  // asking. Editing the tab pushes instantly; this is the other direction, and
  // the hourly GitHub job behind it is what still runs when this project has
  // spent its quota.
  ScriptApp.newTrigger('pullCoupons').timeBased().everyMinutes(30).create();
  Logger.log('Triggers installed: onEdit sync + daily 10:00 scrape + 12:00 watchdog + 1-min fulfillment tick + 30-min coupon pull + weekly backup');
}

// -- helpers ---------------------------------------------------------
function alert_(subject, body) {
  try {
    MailApp.sendEmail(ALERT_EMAIL, '\u26a0\ufe0f Waverole sync: ' + subject,
      body + '\n\n(\u05d4\u05d5\u05d3\u05e2\u05d4 \u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9\u05ea \u05de\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05e9\u05dc \u05d8\u05d1\u05dc\u05ea \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd)');
  } catch (e) { Logger.log('alert email failed: ' + e); }
}

// Positive daily confirmation - sent when the morning check passed, so a
// silent inbox never leaves you guessing whether the check ran at all.
function report_(subject, body) {
  try {
    MailApp.sendEmail(ALERT_EMAIL, '\u2705 Waverole sync: ' + subject,
      body + '\n\n(\u05d4\u05d5\u05d3\u05e2\u05d4 \u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9\u05ea \u05de\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05e9\u05dc \u05d8\u05d1\u05dc\u05ea \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd)');
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
    const msg = '\u05e2\u05de\u05d5\u05d3\u05d5\u05ea \u05d7\u05e1\u05e8\u05d5\u05ea \u05d1\u05d8\u05d1\u05dc\u05ea \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd: ' + missing.join(', ') +
      '\n\u05db\u05e0\u05e8\u05d0\u05d4 \u05e9\u05d5\u05e0\u05d4 \u05e9\u05dd \u05e9\u05dc \u05db\u05d5\u05ea\u05e8\u05ea. \u05e9\u05de\u05d5\u05ea \u05e9\u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u05de\u05db\u05d9\u05e8: ' +
      missing.map(f => HEADERS[f].join(' / ')).join(' | ') +
      '\n\u05d9\u05e9 \u05dc\u05e2\u05d3\u05db\u05df \u05d0\u05ea HEADERS \u05d1\u05e7\u05d5\u05d3 \u05d0\u05d5 \u05dc\u05d4\u05d7\u05d6\u05d9\u05e8 \u05d0\u05ea \u05e9\u05dd \u05d4\u05e2\u05de\u05d5\u05d3\u05d4.';
    alert_('\u05e2\u05de\u05d5\u05d3\u05d4 \u05d7\u05e1\u05e8\u05d4 \u2014 \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05e0\u05e2\u05e6\u05e8', msg);
    throw new Error(msg);
  }
  return map;
}

// Did the Hebrew column names survive the trip into the editor?
//
// The Apps Script editor lays RTL text out inside LTR code, so every Hebrew
// header in HEADERS above renders scrambled - cosmetic, but it makes "did my
// paste arrive intact?" unanswerable by eye. Worse, only 'sku' and 'price' are
// REQUIRED: a mangled source or chosen header does not throw, it silently reads as
// absent, and pickRow_() then falls back to esim.dog for every SKU. Safe, but
// indistinguishable from working.
//
// So: Run this and read NUMBERS. Every column must report a number. A `-1`
// means that header did not match the sheet, and the paste is the suspect.
function checkColumns() {
  const sheet = sheet_();
  const head = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(h => String(h).trim());
  const lines = [];
  for (const [key, names] of Object.entries(HEADERS)) {
    let at = -1;
    for (const name of names) { const i = head.indexOf(name); if (i >= 0) { at = i; break; } }
    lines.push('  ' + key + ' -> column ' + (at < 0 ? 'NOT FOUND' : at + 1 + ' (' + colLetter_(at) + ')'));
  }
  // And what the sheet actually holds, so the tick can be seen without reading
  // a single Hebrew character.
  const map = colMap_(sheet);
  const data = sheet.getDataRange().getValues();
  const bySupplier = {}, ticked = {};
  for (let r = 1; r < data.length; r++) {
    const pkg = rowToPackage_(data[r], map);
    if (!pkg) continue;
    bySupplier[pkg.source] = (bySupplier[pkg.source] || 0) + 1;
    if (pkg._chosen) ticked[pkg.source] = (ticked[pkg.source] || 0) + 1;
  }
  const sold = {};
  for (const pkg of buildPackages_(null)) sold[pkg.source] = (sold[pkg.source] || 0) + 1;
  Logger.log('COLUMNS\n' + lines.join('\n') +
    '\n\nPRICED ROWS PER SUPPLIER: ' + JSON.stringify(bySupplier) +
    '\nOF THOSE, TICKED: ' + JSON.stringify(ticked) +
    '\n\nWHAT THE SITE WOULD BE SENT: ' + JSON.stringify(sold));
}

function colLetter_(i) {
  let s = '';
  for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + (i - 1) % 26) + s;
  return s;
}

function num_(v) {
  const n = parseFloat(String(v).replace(/[^\d.]/g, ''));
  return isNaN(n) ? null : n;
}

function rowToPackage_(row, map) {
  const sku = String(row[map.sku] || '').trim();
  if (!sku || sku.indexOf('.') < 0) return null;    // not a package row
  // A SKU can occupy more than one row: the sheet stacks the same package
  // as each supplier sells it - esim.dog's row and Stellar's row, one above
  // the other, under one code. Which of them the site sells is decided in
  // pickRow_(), by the tick in the chosen column; here every priced row is a candidate.
  // A blank source column means esim.dog, as it does for the scraper and the bot.
  const src = String(row[map.source] || '').trim().toLowerCase() || 'esim.dog';
  // A row with no customer price is a note to ourselves, not a product. Every
  // one of the 82 live esim.dog rows carries one, so this turns nothing off
  // today; what it buys is a comparison row that CANNOT become a storefront
  // entry by accident. Before this, a priceless row was still posted - sku,
  // GB, days and in_stock=true, just no price - which is how a 50GB Germany
  // package nobody had priced yet would have appeared on the site the moment
  // it was written down next to the one we actually sell.
  const price = num_(row[map.price]);
  if (price === null) return null;
  const pkg = { sku: sku, source: src };
  pkg._chosen = map.chosen !== undefined && String(row[map.chosen] || '').trim() !== '';
  pkg.price = price;
  pkg.sale = num_(row[map.sale]) || 0;
  pkg.in_stock = String(row[map.stock] || '').trim() === '';
  const days = num_(row[map.days]); if (days !== null) pkg.days = days;
  const gb   = num_(row[map.gb]);   if (gb   !== null) pkg.gb = gb;
  const net = String(row[map.networks] || '').replace(/^Networks\s*\u2022\s*/i, '').trim();
  if (net) pkg.networks = net;
  const bip = String(row[map.breakout_ip] || '').trim();
  if (bip) pkg.breakout_ip = bip;
  const fee = num_(row[map.fee]); if (fee !== null) pkg.fee = fee;
  return pkg;
}

// The one row of a SKU's pair the site sells. The (tick) wins when exactly one
// row carries it; otherwise esim.dog's row, the supplier we have always
// bought from. A ticked Stellar row with no customer price never gets here
// (rowToPackage_ dropped it), so the tick alone cannot empty a SKU. The site
// still refuses checkout on a supplier its bot cannot buy from - the tick
// moves the PRICE, LIVE_SUPPLIERS on the site moves the money.
function pickRow_(cands) {
  const ticked = cands.filter(p => p._chosen);
  const pick = ticked.length === 1 ? ticked[0] : cands.find(p => p.source === 'esim.dog');
  if (!pick) return null;
  const pkg = Object.assign({}, pick);
  delete pkg._chosen;
  return pkg;
}

function buildPackages_(rowsWanted) {   // rowsWanted: null = all, or Set of sheet row numbers
  const sheet = sheet_();
  const map = colMap_(sheet);
  const data = sheet.getDataRange().getValues();
  // Always read the whole sheet, even for an edit to one row: the row that
  // changed may be the ticked Stellar half of a pair, and choosing between
  // the halves needs both of them in hand.
  const bySku = {};
  const touched = new Set();
  for (let r = 1; r < data.length; r++) {
    const pkg = rowToPackage_(data[r], map);
    if (!pkg) continue;
    (bySku[pkg.sku] = bySku[pkg.sku] || []).push(pkg);
    if (!rowsWanted || rowsWanted.has(r + 1)) touched.add(pkg.sku);
  }
  const out = [];
  for (const sku of Object.keys(bySku)) {
    if (!touched.has(sku)) continue;
    const pick = pickRow_(bySku[sku]);
    if (pick) out.push(pick);
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
  if (!token) throw new Error('\u05d7\u05e1\u05e8 UPDATE_PACKAGES_TOKEN \u05d1-Script Properties (\u05d4\u05d2\u05d3\u05e8\u05d5\u05ea \u05d4\u05e4\u05e8\u05d5\u05d9\u05e7\u05d8)');
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
    alert_('\u05e9\u05dc\u05d9\u05d7\u05ea \u05e2\u05d3\u05db\u05d5\u05df \u05dc\u05d0\u05ea\u05e8 \u05e0\u05db\u05e9\u05dc\u05d4 (HTTP ' + code + ')',
      '\u05d4\u05e7\u05e8\u05d9\u05d0\u05d4 \u05dc-' + ENDPOINT + ' \u05d4\u05d7\u05d6\u05d9\u05e8\u05d4 ' + code + ':\n' + body.slice(0, 500));
    throw new Error('update-packages HTTP ' + code);
  }
  let msg = 'HTTP ' + code;
  try {
    const j = JSON.parse(body);
    msg = '\u05e2\u05d5\u05d3\u05db\u05e0\u05d5 ' + (j.updated || []).length +
      ((j.not_found || []).length ? ' | \u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d5: ' + j.not_found.join(', ') : '') +
      ((j.warnings || []).length ? ' | \u26a0\ufe0f ' + j.warnings.length + ' \u05d0\u05d6\u05d4\u05e8\u05d5\u05ea' : '');
  } catch (err) {}
  Logger.log(msg);
  // Freshness signal for the watchdog: a successful POST means the site HAS
  // today's prices even when nothing changed (the endpoint then skips the
  // commit, so the overlay's `updated` timestamp does NOT move - that false
  // alarm is exactly what fired on 2026-07-16).
  PropertiesService.getScriptProperties()
    .setProperty('LAST_SYNC_OK', new Date().toISOString());
  try { SpreadsheetApp.openById(SHEET_ID).toast(msg, 'Waverole', 8); } catch (e) {}
  return body;
}

// -- actions ---------------------------------------------------------
// Editing a run of cells used to fire one full site push PER EDIT. A few
// minutes of ordinary work on the sheet produced 28 overlapping runs, one of
// which hung for 175 seconds - and because Apps Script caps how much runs at
// once, that flood starved the every-minute fulfilment dispatcher, which is
// what makes a customer's eSIM late.
//
// So an edit now queues its row and tries to flush straight away. If another
// flush already holds the lock it just leaves the row queued: whoever is
// flushing, or the next minute tick, will send it. Isolated edits stay
// instant, a burst collapses into one push, and nothing piles up.
const PENDING_ROWS_KEY = 'PENDING_SYNC_ROWS';

// One tick per SKU, and the colours follow the tick.
//
// The owner ticks the Stellar row of a pair -> the esim.dog row loses its
// tick and turns grey, the Stellar row turns green. Runs BEFORE the sync
// reads the sheet, because pickRow_() resolves a two-tick pair to esim.dog -
// the opposite of what was just asked for - and would push it before anyone
// noticed. Programmatic writes do not re-fire onEdit, so this cannot loop.
// Clearing a tick greys that row and changes nothing else (the site then
// falls back to esim.dog for the SKU, as it always has).
// Only a single-cell edit is handled: a pasted block that ticks both halves
// of a pair has no "the one the owner meant", so it is left exactly as typed
// and checkColumns will name it.
function enforceChoice_(sheet, map, e) {
  if (map.chosen === undefined || map.sku === undefined) return;
  const col = map.chosen + 1;
  if (e.range.getColumn() !== col || e.range.getNumRows() !== 1 ||
      e.range.getNumColumns() !== 1) return;
  const row = e.range.getRow();
  if (row < 2) return;
  const lastCol = Math.max.apply(null, Object.values(map)) + 1;   // paint A..last synced column only
  const data = sheet.getRange(1, 1, sheet.getLastRow(), lastCol).getValues();
  const sku = String(data[row - 1][map.sku] || '').trim();
  if (!sku || sku.indexOf('.') < 0) return;
  const ticked = String(data[row - 1][map.chosen] || '').trim() !== '';
  if (!ticked) {
    sheet.getRange(row, 1, 1, lastCol).setBackground(ROW_UNCHOSEN_BG);
    SpreadsheetApp.flush();
    return;
  }
  for (let r = 2; r <= data.length; r++) {
    if (String(data[r - 1][map.sku] || '').trim() !== sku) continue;
    const mine = r === row;
    if (!mine && String(data[r - 1][map.chosen] || '').trim() !== '') {
      sheet.getRange(r, col).clearContent();
    }
    sheet.getRange(r, 1, 1, lastCol).setBackground(mine ? ROW_CHOSEN_BG : ROW_UNCHOSEN_BG);
  }
  SpreadsheetApp.flush();   // the sync below must read the one-tick state
}

function onEditPush(e) {
  try {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet();
    // The coupon tab is answered first and on its own terms: it has no SKUs, no
    // fee ladder and no chosen-supplier tick, so everything below would read its
    // header row as a broken price sheet.
    if (sheet.getName() === COUPON_TAB) { syncCoupons_(true); return; }
    const main = e.source.getSheets()[0];
    if (sheet.getSheetId() !== main.getSheetId()) return;
    const map = colMap_(sheet);
    const watched = Object.values(map).map(i => i + 1);
    const c1 = e.range.getColumn(), c2 = e.range.getLastColumn();
    if (!watched.some(c => c >= c1 && c <= c2)) return;   // not a synced column
    const reached = applyFee_(sheet, map, e) || [];
    enforceChoice_(sheet, map, e);
    const rows = [];
    for (let r = Math.max(2, e.range.getRow()); r <= e.range.getLastRow(); r++) rows.push(r);
    // The twin rows applyFee_ just rewrote. buildPackages_ turns a row into
    // its SKU and then picks the ticked half of the pair, so it is enough
    // that ONE row of each touched SKU is in here - but sending them all
    // costs nothing (they collapse to one package per SKU) and leaves no
    // room for the pick to land on a row nobody queued.
    reached.forEach(function (r) { if (r >= 2 && rows.indexOf(r) < 0) rows.push(r); });
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

// -- receipts sheet -> customer's usage meter, instantly --------------
// The daily usage bot refreshes every live package once a day. This closes
// the gap in between: edit the consumption cell in the receipts sheet and the
// customer's order page shows the new figure within seconds - the same
// arrangement the price sheet has with the shop.
//
// Only MANUAL edits reach here; Apps Script does not fire onEdit for writes
// made by a script, so the daily bot is not double-counted (it pushes to the
// site directly anyway).
const RCPT_USAGE_COL = 'GB (0/X) - \u05e0\u05d9\u05e6\u05d5\u05dc';
const RCPT_ORDER_COL = '\u05de\u05e1\u05f3 \u05d4\u05d6\u05de\u05e0\u05d4';

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
      // nothing, looks exactly like a broken sheet, and gives no clue why -
      // which is how it sat unnoticed. One email, at most once a day, then
      // back to quiet.
      const props = PropertiesService.getScriptProperties();
      const today = Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'yyyy-MM-dd');
      if (props.getProperty('TOKEN_WARNED_ON') !== today) {
        props.setProperty('TOKEN_WARNED_ON', today);
        alert_('\u05d7\u05e1\u05e8 ORDERS_TOKEN \u2014 \u05de\u05d3 \u05d4\u05e0\u05d9\u05e6\u05d5\u05dc \u05dc\u05d0 \u05de\u05ea\u05e2\u05d3\u05db\u05df',
          '\u05e2\u05e8\u05db\u05ea \u05d0\u05ea \u05e2\u05de\u05d5\u05d3\u05ea \u05d4\u05e0\u05d9\u05e6\u05d5\u05dc \u05d1\u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d1\u05dc\u05d5\u05ea, \u05d0\u05d1\u05dc \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05de\u05d9\u05d9\u05d3\u05d9 \u05dc\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05e8\u05e5 ' +
          '\u05db\u05d9 \u05d0\u05d9\u05df ORDERS_TOKEN \u05d1\u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8.\n\n' +
          '\u05ea\u05d9\u05e7\u05d5\u05df: \u05e2\u05d5\u05e8\u05da \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 \u2699\ufe0f \u05d4\u05d2\u05d3\u05e8\u05d5\u05ea \u05d4\u05e4\u05e8\u05d5\u05d9\u05e7\u05d8 \u2192 \u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 ' +
          '\u05d4\u05d5\u05e1\u05e4\u05ea \u05de\u05d0\u05e4\u05d9\u05d9\u05df \u2192 \u05e9\u05dd: ORDERS_TOKEN \u2192 \u05d4\u05d3\u05d1\u05e7 \u05d0\u05ea \u05d4\u05e2\u05e8\u05da \u2192 \u05e9\u05de\u05d9\u05e8\u05d4.\n' +
          '\u05d0\u05d7\u05e8 \u05db\u05da \u05d4\u05e8\u05e5 checkReceiptsSync \u05db\u05d3\u05d9 \u05dc\u05d5\u05d5\u05d3\u05d0 \u05e9\u05d4\u05db\u05d5\u05dc \u05e2\u05d5\u05d1\u05d3.');
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
 * in plain words - instead of leaving "it just doesn't update" to guesswork.
 */
function checkReceiptsSync() {
  const out = [];
  const ok = (s) => out.push('\u2705 ' + s);
  const bad = (s) => out.push('\u274c ' + s);

  const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
  if (tok) ok('ORDERS_TOKEN \u05e7\u05d9\u05d9\u05dd \u05d1\u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 (' + tok.length + ' \u05ea\u05d5\u05d5\u05d9\u05dd)');
  else bad('\u05d7\u05e1\u05e8 ORDERS_TOKEN \u2192 \u05d4\u05d2\u05d3\u05e8\u05d5\u05ea \u05d4\u05e4\u05e8\u05d5\u05d9\u05e7\u05d8 \u2699\ufe0f \u2192 \u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 ' +
           '\u05e9\u05dd: ORDERS_TOKEN, \u05e2\u05e8\u05da: \u05d4\u05d8\u05d5\u05e7\u05df \u05e9\u05dc \u05d4\u05d0\u05ea\u05e8');

  const trig = ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'onReceiptsEdit');
  if (trig.length) ok('\u05d4\u05d8\u05e8\u05d9\u05d2\u05e8 onReceiptsEdit \u05de\u05d5\u05ea\u05e7\u05df (' + trig.length + ')');
  else bad('\u05d4\u05d8\u05e8\u05d9\u05d2\u05e8 onReceiptsEdit \u05dc\u05d0 \u05de\u05d5\u05ea\u05e7\u05df \u2192 \u05d4\u05e8\u05e5 setupTriggers \u05de\u05d4\u05ea\u05e4\u05e8\u05d9\u05d8');

  let sh = null;
  try {
    sh = SpreadsheetApp.openById(RECEIPTS_ID).getSheets()[0];
    ok('\u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d1\u05dc\u05d5\u05ea \u05e0\u05e4\u05ea\u05d7\u05ea: "' + sh.getName() + '"');
  } catch (e) {
    bad('\u05d0\u05d9\u05df \u05d2\u05d9\u05e9\u05d4 \u05dc\u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d1\u05dc\u05d5\u05ea: ' + e);
  }

  let sample = null;
  if (sh) {
    const hdr = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].map(h => String(h).trim());
    const uc = hdr.indexOf(RCPT_USAGE_COL) + 1, oc = hdr.indexOf(RCPT_ORDER_COL) + 1;
    if (uc) ok('\u05e2\u05de\u05d5\u05d3\u05ea \u05d4\u05e0\u05d9\u05e6\u05d5\u05dc "' + RCPT_USAGE_COL + '" \u05e0\u05de\u05e6\u05d0\u05d4 (\u05e2\u05de\u05d5\u05d3\u05d4 ' + uc + ')');
    else bad('\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d4 \u05e2\u05de\u05d5\u05d3\u05d4 \u05d1\u05e9\u05dd "' + RCPT_USAGE_COL + '" \u2014 \u05e9\u05d9\u05e0\u05d5\u05d9 \u05e9\u05dd \u05d4\u05db\u05d5\u05ea\u05e8\u05ea \u05de\u05e0\u05ea\u05e7 \u05d0\u05ea \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df');
    if (oc) ok('\u05e2\u05de\u05d5\u05d3\u05ea \u05de\u05e1\u05e4\u05e8 \u05d4\u05d4\u05d6\u05de\u05e0\u05d4 \u05e0\u05de\u05e6\u05d0\u05d4 (\u05e2\u05de\u05d5\u05d3\u05d4 ' + oc + ')');
    else bad('\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d4 \u05e2\u05de\u05d5\u05d3\u05d4 \u05d1\u05e9\u05dd "' + RCPT_ORDER_COL + '"');

    if (uc && oc) {
      const last = sh.getLastRow();
      for (let r = 2; r <= last; r++) {
        const id = String(sh.getRange(r, oc).getValue()).trim();
        const m = String(sh.getRange(r, uc).getValue()).match(/([\d.]+)\s*\/\s*([\d.]+)/);
        if (id && m) { sample = { row: r, id: id, used: parseFloat(m[1]), total: parseFloat(m[2]) }; break; }
      }
      if (sample) ok('\u05e9\u05d5\u05e8\u05d4 \u05dc\u05d3\u05d5\u05d2\u05de\u05d4: ' + sample.id + ' = ' + sample.used + '/' + sample.total + ' GB (\u05e9\u05d5\u05e8\u05d4 ' + sample.row + ')');
      else out.push('\u2139\ufe0f \u05d0\u05d9\u05df \u05e2\u05d3\u05d9\u05d9\u05df \u05e9\u05d5\u05e8\u05d4 \u05e2\u05dd \u05e0\u05d9\u05e6\u05d5\u05dc \u05d1\u05e4\u05d5\u05e8\u05de\u05d8 "0.4 / 1" \u2014 \u05dc\u05db\u05df \u05d0\u05d9\u05df \u05de\u05d4 \u05dc\u05e9\u05dc\u05d5\u05d7');
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
      if ((body.updated || []).length) ok('\u05d4\u05d0\u05ea\u05e8 \u05e2\u05d5\u05d3\u05db\u05df \u05d1\u05d4\u05e6\u05dc\u05d7\u05d4 \u05e2\u05d1\u05d5\u05e8 ' + sample.id + ' \u2014 \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05e2\u05d5\u05d1\u05d3 \u05de\u05e7\u05e6\u05d4 \u05dc\u05e7\u05e6\u05d4');
      else if ((body.not_found || []).length) bad('\u05d4\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05de\u05db\u05d9\u05e8 \u05d0\u05ea \u05d4\u05d4\u05d6\u05de\u05e0\u05d4 ' + sample.id + ' (\u05d9\u05d9\u05ea\u05db\u05df \u05e9\u05e0\u05de\u05d7\u05e7\u05d4 \u05d0\u05d5 \u05d9\u05e9\u05e0\u05d4 \u05de-90 \u05d9\u05d5\u05dd)');
      else out.push('\u2139\ufe0f \u05d4\u05d0\u05ea\u05e8 \u05e2\u05e0\u05d4 200 \u05d1\u05dc\u05d9 \u05dc\u05e2\u05d3\u05db\u05df: ' + txt.slice(0, 200));
    } else if (code === 401 || code === 403) {
      bad('\u05d4\u05d0\u05ea\u05e8 \u05d3\u05d7\u05d4 \u05d0\u05ea \u05d4\u05d8\u05d5\u05e7\u05df (' + code + ') \u2014 \u05d4-ORDERS_TOKEN \u05db\u05d0\u05df \u05e9\u05d5\u05e0\u05d4 \u05de\u05d6\u05d4 \u05e9\u05d1\u05d0\u05ea\u05e8');
    } else {
      bad('\u05d4\u05d0\u05ea\u05e8 \u05d4\u05d7\u05d6\u05d9\u05e8 ' + code + ': ' + txt.slice(0, 200));
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
  if (!token) throw new Error('\u05d7\u05e1\u05e8 GH_TOKEN \u05d1-Script Properties (\u05d4\u05d2\u05d3\u05e8\u05d5\u05ea \u05d4\u05e4\u05e8\u05d5\u05d9\u05e7\u05d8)');
  const res = UrlFetchApp.fetch(GH_DISPATCH, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });
  const ok = res.getResponseCode() === 204;
  if (!ok) alert_('\u05d4\u05e4\u05e2\u05dc\u05ea \u05d4\u05e1\u05e7\u05e8\u05d9\u05d9\u05e4\u05e8 \u05e0\u05db\u05e9\u05dc\u05d4', res.getContentText().slice(0, 500));
  Logger.log(ok ? '\u05d4\u05e1\u05e8\u05d9\u05e7\u05d4 \u05d4\u05d5\u05e4\u05e2\u05dc\u05d4 \u05d1-GitHub \u2713' : '\u05e9\u05d2\u05d9\u05d0\u05d4: ' + res.getContentText());
}

// -- fulfillment bot dispatcher - every 5 minutes --------------------
// GitHub throttles scheduled workflows on public repos (observed: */5 cron
// firing ~once an hour). Apps Script triggers are punctual, so this tick
// dispatches the fulfillment workflow instead. Costs ~1s per run - far
// inside the daily trigger quota. Failures alert at most once per 6h.
function fulfillmentTick() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GH_TOKEN');
  if (!token) return;                        // not configured - GitHub cron still runs

  // The trigger fires every MINUTE, but dispatching every minute would mean
  // 1440 Actions runs a day for an inbox that is empty almost all the time.
  // So: dispatch at once while a paid order is still waiting for its eSIM
  // (customer gets the QR in ~1 minute instead of up to 5), and otherwise
  // keep the old 5-minute cadence - same idle cost as before.
  // Finish any order whose eSIM was still being provisioned when the purchase
  // bot handed over its supplier session. Usually a no-op - the site normally
  // completes the order on the spot - but it is what closes the gap when the
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
    if (res.getResponseCode() === 204) return;         // dispatched (tick)
    throw new Error('HTTP ' + res.getResponseCode() + ': ' +
      res.getContentText().slice(0, 300));
  } catch (err) {
    const last = +(props.getProperty('FT_LAST_ALERT') || 0);
    if (Date.now() - last > 6 * 36e5) {
      props.setProperty('FT_LAST_ALERT', String(Date.now()));
      alert_('\u05d4\u05e4\u05e2\u05dc\u05ea \u05d1\u05d5\u05d8 \u05d4\u05de\u05d9\u05de\u05d5\u05e9 \u05de\u05d4-Apps Script \u05e0\u05db\u05e9\u05dc\u05ea',
        String(err) + '\n(\u05d4\u05d1\u05d5\u05d8 \u05e2\u05d3\u05d9\u05d9\u05df \u05e8\u05e5 \u05de\u05d4-cron \u05e9\u05dc GitHub, \u05e8\u05e7 \u05dc\u05d0\u05d8 \u05d9\u05d5\u05ea\u05e8. ' +
        '\u05d4\u05ea\u05e8\u05d0\u05d4 \u05d6\u05d5 \u05e0\u05e9\u05dc\u05d7\u05ea \u05dc\u05db\u05dc \u05d4\u05d9\u05d5\u05ea\u05e8 \u05e4\u05e2\u05dd \u05d1-6 \u05e9\u05e2\u05d5\u05ea.)');
    }
    Logger.log('fulfillmentTick failed: ' + err);
  }
}

// Receipts columns (1-based) the tick reads. The purchase bot appends a row
// the moment it PAYS; the fulfillment bot later fills the activation code in
// from esim.dog's delivery email. A row with an order id and no activation
// code is therefore an order mid-flight.
const RCP_DATE_COL = 2;         // date
const RCP_ORDER_COL = 6;        // order number
const RCP_ACTIVATION_COL = 8;   // Activation Code
const AWAITING_WINDOW_MS = 30 * 60 * 1000;

function rowTime_(v) {
  if (v instanceof Date) return v.getTime();
  // The bot writes DD/MM/YYYY HH:MM:SS - day first, so Date.parse would read
  // 07/12 as 7 December in some locales and 12 July in others. Parse it by hand.
  const m = String(v).match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return NaN;
  return new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5], +(m[6] || 0)).getTime();
}

// -- supplier watch - every minute -----------------------------------
// On 2026-07-27 the supplier answered HTTP 200 on every page while all of its
// JavaScript build files 404'd: the site rendered, the Checkout button did
// nothing, and nobody could buy. We could still have taken payments for
// packages we had no way to obtain.
//
// So this asks the site to re-run its real check (page loads AND its build
// files exist), which both keeps the cached verdict warm for shoppers and
// tells us the moment selling becomes impossible - or possible again.
// Emails only on a CHANGE, so a long outage does not send 1440 messages.
function supplierWatch_() {
  const props = PropertiesService.getScriptProperties();
  const tok = props.getProperty('ORDERS_TOKEN');
  if (!tok) return;                          // not configured - site self-checks

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
  if (was === now) return;                   // nothing changed - stay quiet
  props.setProperty('SUPPLIER_SELLING', now);
  if (was === null) return;                  // first ever run - no news yet

  if (!selling) {
    alert_('\u05d4\u05e1\u05e4\u05e7 \u05dc\u05d0 \u05d6\u05de\u05d9\u05df \u2014 \u05d4\u05de\u05db\u05d9\u05e8\u05d5\u05ea \u05e0\u05e2\u05e6\u05e8\u05d5 \u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9\u05ea',
      '\u05dc\u05d0 \u05e0\u05d9\u05ea\u05df \u05dc\u05e8\u05db\u05d5\u05e9 \u05d7\u05d1\u05d9\u05dc\u05d5\u05ea \u05de\u05d4\u05e1\u05e4\u05e7 \u05db\u05e8\u05d2\u05e2, \u05d5\u05dc\u05db\u05df \u05d4\u05d0\u05ea\u05e8 \u05e2\u05d1\u05e8 \u05dc\u05de\u05e6\u05d1 \u05ea\u05d7\u05d6\u05d5\u05e7\u05d4 \u05d5\u05d0\u05d9 \u05d0\u05e4\u05e9\u05e8 \u05dc\u05e7\u05e0\u05d5\u05ea \u05d1\u05d5.\n\n' +
      '\u05e1\u05d9\u05d1\u05d4: ' + reason + '\n\n' +
      '\u05d4\u05d6\u05de\u05e0\u05d5\u05ea \u05e7\u05d9\u05d9\u05de\u05d5\u05ea \u05de\u05de\u05e9\u05d9\u05db\u05d5\u05ea \u05dc\u05e4\u05e2\u05d5\u05dc \u05db\u05e8\u05d2\u05d9\u05dc \u2014 \u05e8\u05e7 \u05de\u05db\u05d9\u05e8\u05d5\u05ea \u05d7\u05d3\u05e9\u05d5\u05ea \u05de\u05d5\u05e9\u05d1\u05ea\u05d5\u05ea.\n' +
      '\u05d4\u05d0\u05ea\u05e8 \u05d9\u05d9\u05e4\u05ea\u05d7 \u05de\u05d7\u05d3\u05e9 \u05de\u05e2\u05e6\u05de\u05d5 \u05ea\u05d5\u05da \u05db\u05d3\u05e7\u05d4 \u05de\u05e8\u05d2\u05e2 \u05e9\u05d4\u05e1\u05e4\u05e7 \u05d9\u05d7\u05d6\u05d5\u05e8.');
  } else {
    // Buying works again - hand back every order that was paid for but could
    // not be bought while the supplier was down, before saying all is well.
    const rescued = retryUnfulfilled_(tok);
    report_('\u05d4\u05e1\u05e4\u05e7 \u05d7\u05d6\u05e8 \u2014 \u05d4\u05de\u05db\u05d9\u05e8\u05d5\u05ea \u05e0\u05e4\u05ea\u05d7\u05d5 \u05de\u05d7\u05d3\u05e9',
      '\u05e0\u05d9\u05ea\u05df \u05e9\u05d5\u05d1 \u05dc\u05e8\u05db\u05d5\u05e9 \u05d7\u05d1\u05d9\u05dc\u05d5\u05ea \u05de\u05d4\u05e1\u05e4\u05e7, \u05d5\u05d4\u05d0\u05ea\u05e8 \u05d7\u05d6\u05e8 \u05dc\u05e4\u05e2\u05d5\u05dc\u05d4 \u05e8\u05d2\u05d9\u05dc\u05d4.' +
      (rescued ? '\n\n\u05d4\u05d5\u05d7\u05d6\u05e8\u05d5 \u05dc\u05ea\u05d5\u05e8 ' + rescued + ' \u05d4\u05d6\u05de\u05e0\u05d5\u05ea \u05e9\u05e9\u05d5\u05dc\u05de\u05d5 \u05d5\u05dc\u05d0 \u05e1\u05d5\u05e4\u05e7\u05d5 \u05d1\u05d6\u05de\u05df \u05d4\u05ea\u05e7\u05dc\u05d4.' : ''));
  }
}

// Give paid-but-unbought orders back to the bot. Returns how many.
// Orders that have used up their retries are NOT returned here - the site
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
  if (!tok) return;                          // optional - see setup notes above
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
  // Signal 1 - the SITE's own queue: an order sits there as "pending" from
  // the second the payment IPN lands, before the PC bot has done anything.
  // The receipts-row signal below only exists AFTER the PC bot both bought
  // and wrote the row - the night WR-845JFY got stuck proved that row can
  // simply never appear. Optional: needs ORDERS_TOKEN in Script Properties
  // (same value as the site's env var); skipped silently without it.
  try {
    const tok = PropertiesService.getScriptProperties().getProperty('ORDERS_TOKEN');
    if (tok) {
      const res = UrlFetchApp.fetch('https://www.waverole.com/api/orders?status=pending&probe=1', {
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
  // Signal 2 - a receipts row with an order number and no activation code
  // (order bought, eSIM email not yet processed).
  try {
    const sh = SpreadsheetApp.openById(RECEIPTS_ID).getSheets()[0];
    const last = sh.getLastRow();
    if (last < 2) return false;
    const n = Math.min(15, last - 1);        // newest rows only - enough for any burst
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
    // Never let this gate break the dispatcher - fall back to the 5-min cadence.
    Logger.log('orderAwaitingEsim_ failed: ' + err);
  }
  return false;
}

function dailyScrape() {
  // Dispatching the GitHub scraper needs a GH_TOKEN. Without one this step
  // is SKIPPED SILENTLY - the scraper has its own daily schedule on GitHub,
  // so no alert is needed (it used to email an error every morning).
  const gh = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (gh) {
    try {
      runScrapeNow();
    } catch (err) {
      alert_('dailyScrape \u05e0\u05db\u05e9\u05dc', String(err));
    }
  } else {
    Logger.log('GH_TOKEN not set \u2014 skipping dispatch (GitHub cron handles the scrape).');
  }
  // Full site sync 70 min later - after the scraper wrote fresh data to the
  // sheet. Programmatic writes don't fire onEdit, so this sync is the ONLY
  // path that gets the daily price changes to the site.
  //
  // 45 minutes was measured against the old scrape. The job now runs up to
  // ~68 minutes on its own budget and is followed by the Stellar pass and
  // the supplier chooser, so a 45-minute wait would sync the sheet halfway
  // through the rewrite - yesterday's prices for whatever had not landed
  // yet. Syncing late costs nothing; syncing early publishes a half-written
  // catalogue.
  ScriptApp.newTrigger('fullSyncOnce').timeBased().after(70 * 60 * 1000).create();
}

function fullSyncOnce() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'fullSyncOnce')
    .forEach(t => ScriptApp.deleteTrigger(t));
  try {
    fullSync();
  } catch (err) {
    alert_('\u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05d9\u05d5\u05de\u05d9 \u05d4\u05de\u05dc\u05d0 \u05e0\u05db\u05e9\u05dc', String(err));
  }
}

// -- weekly Drive backup of both spreadsheets ------------------------
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
    Logger.log('weekly backup done \u2192 Drive folder "' + BACKUP_FOLDER + '"');
  } catch (err) {
    alert_('\u05d4\u05d2\u05d9\u05d1\u05d5\u05d9 \u05d4\u05e9\u05d1\u05d5\u05e2\u05d9 \u05e9\u05dc \u05d4\u05d8\u05d1\u05dc\u05d0\u05d5\u05ea \u05e0\u05db\u05e9\u05dc', String(err));
  }
}

// Manual test: verifies the alert-email path works (run from the editor).
function testAlert() {
  alert_('\u05d1\u05d3\u05d9\u05e7\u05ea \u05de\u05e2\u05e8\u05db\u05ea \u05d4\u05d4\u05ea\u05e8\u05d0\u05d5\u05ea',
    '\u05d0\u05dd \u05e7\u05d9\u05d1\u05dc\u05ea \u05d0\u05ea \u05d4\u05de\u05d9\u05d9\u05dc \u05d4\u05d6\u05d4 \u2014 \u05de\u05e2\u05e8\u05db\u05ea \u05d4\u05d4\u05ea\u05e8\u05d0\u05d5\u05ea \u05e9\u05dc \u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd \u05e2\u05d5\u05d1\u05d3\u05ea \u2713');
  Logger.log('test alert sent to ' + ALERT_EMAIL);
}

// Freshness of the SCRAPER's own work, not of the file. Generous because the
// scrape is daily and its stamp carries GitHub's UTC clock: 24h cadence +
// timezone skew + a slow run must not cry wolf, while "did not run at all"
// shows up around 48h.
const MAX_SCRAPE_STALE_HOURS = 30;
const GH_RUNS = 'https://api.github.com/repos/nitzanbarash/esim-price-scraper/actions/workflows/scrape.yml/runs?per_page=5';

// A cell written with USER_ENTERED comes back as a Date when Sheets
// recognised the format and as text when it did not. Handle both rather than
// trusting either.
function toDate_(v) {
  // Duck-typed, not `instanceof Date`: values handed over by the Sheets
  // service can come from another JS realm, where instanceof silently
  // answers false - and a date read as "no date" would mail a stale-data
  // alarm every single morning. getTime also screens out Invalid Date.
  if (v && typeof v.getTime === 'function') {
    return isNaN(v.getTime()) ? null : v;
  }
  const m = String(v == null ? '' : v).trim()
    .match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null;
}

/**
 * How fresh is the data the scraper actually wrote?
 *
 * The old check asked Drive for the FILE's last-modified time, which moves
 * whenever anyone touches the spreadsheet - including the owner's own edits.
 * On 2026-08-19 every scrape run was killed by the workflow time limit and
 * this still reported healthy, because the file had been edited by hand that
 * morning. So read the scraper's OWN per-row stamp instead: it is the only
 * value nothing but a completed scrape can produce.
 *
 * Per-row rather than newest-row: the scraper now saves in batches, so a run
 * cut off half way leaves some rows fresh and the rest a day old - a state
 * the newest stamp alone would report as perfect.
 *
 * Columns are found by header text here rather than through HEADERS, so the
 * sync payload cannot be changed by accident from this side.
 */
function scraperFreshness_() {
  const values = sheet_().getDataRange().getValues();
  const head = values[0].map(h => String(h).trim());
  const iLink = head.indexOf('\u05e7\u05d9\u05e9\u05d5\u05e8');
  const iUpd = head.indexOf('\u05e2\u05d5\u05d3\u05db\u05df \u05dc\u05d0\u05d7\u05e8\u05d5\u05e0\u05d4');
  if (iLink < 0 || iUpd < 0) {
    return { error: '\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d4 \u05e2\u05de\u05d5\u05d3\u05ea "\u05e7\u05d9\u05e9\u05d5\u05e8" \u05d0\u05d5 "\u05e2\u05d5\u05d3\u05db\u05df \u05dc\u05d0\u05d7\u05e8\u05d5\u05e0\u05d4" \u05d1\u05e9\u05d5\u05e8\u05ea \u05d4\u05db\u05d5\u05ea\u05e8\u05d5\u05ea' };
  }

  const cutoff = Date.now() - MAX_SCRAPE_STALE_HOURS * 36e5;
  let total = 0, stale = 0, blank = 0, oldest = null;
  for (let r = 1; r < values.length; r++) {
    // Only rows the scraper is responsible for. A row with no link is never
    // stamped, and counting it would make every sheet permanently "stale".
    if (String(values[r][iLink] || '').indexOf('http') !== 0) continue;
    total++;
    const d = toDate_(values[r][iUpd]);
    if (!d) { blank++; continue; }
    if (d.getTime() < cutoff) stale++;
    if (oldest === null || d < oldest) oldest = d;
  }
  return { total: total, stale: stale, blank: blank, oldest: oldest };
}

/** The newest FINISHED run of the scrape workflow, or null if unknowable. */
function lastScrapeRun_() {
  // esim-price-scraper is a PUBLIC repo, so its run history needs no token.
  // The first version bailed out when GH_TOKEN was unset and returned "no
  // problem found" - a check that quietly declines to run, which is the same
  // shape of bug as the all-clear this whole function exists to prevent.
  // The token is sent when present only because it raises the rate limit.
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  const headers = { Accept: 'application/vnd.github+json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = UrlFetchApp.fetch(GH_RUNS, { headers: headers, muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) {
    throw new Error('GitHub API \u05d4\u05d7\u05d6\u05d9\u05e8 ' + res.getResponseCode());
  }
  const runs = JSON.parse(res.getContentText()).workflow_runs || [];
  for (let i = 0; i < runs.length; i++) {
    if (runs[i].status === 'completed') return runs[i];
  }
  return null;                           // genuinely no finished run yet
}

// -- watchdog: is the live site actually fresh? ----------------------
// The handlers that must be installed for the automation to exist at all.
// Kept next to the watchdog rather than inside setupTriggers so that adding a
// feature here forces the question "and is it actually running?".
const EXPECTED_TRIGGERS = ['onEditPush', 'onReceiptsEdit', 'dailyScrape',
                           'checkSiteFresh', 'fulfillmentTick', 'pullCoupons',
                           'weeklyBackup'];

/**
 * The daily 12:00 health check.
 *
 * Every check appends to ONE list of problems and a single place at the end
 * decides between the alert and the all-clear. That structure is the fix for
 * the failure this function itself had: the checks used to email
 * independently, so the site-freshness check could send its all-clear in the
 * same minute the scraper check found the prices a day old (2026-08-19). An
 * all-clear must be a statement about EVERY check, or it is worse than no
 * email at all - it actively tells the owner to stop looking.
 */
function checkSiteFresh() {
  const problems = [];
  const passed = [];

  // 1. Is the automation even installed?
  //
  // A trigger that was never created fails in the most expensive way there is:
  // in perfect silence. onReceiptsEdit sat missing for days - the code existed,
  // was correct, was tested, and simply had never been deployed, so editing the
  // receipts sheet did nothing and there was nothing anywhere to say why. Newly
  // written code that is never installed looks exactly like broken code.
  try {
    const installed = ScriptApp.getProjectTriggers().map(t => t.getHandlerFunction());
    const absent = EXPECTED_TRIGGERS.filter(f => installed.indexOf(f) < 0);
    if (absent.length) {
      problems.push('\u05d8\u05e8\u05d9\u05d2\u05e8\u05d9\u05dd \u05d7\u05e1\u05e8\u05d9\u05dd \u2014 \u05d7\u05dc\u05e7 \u05de\u05d4\u05d0\u05d5\u05d8\u05d5\u05de\u05e6\u05d9\u05d4 \u05dc\u05d0 \u05e8\u05e6\u05d4 \u05d1\u05db\u05dc\u05dc: ' + absent.join(', ') +
        '\n   \u05db\u05dc \u05e2\u05d5\u05d3 \u05d4\u05dd \u05d7\u05e1\u05e8\u05d9\u05dd \u05d4\u05dd \u05e4\u05e9\u05d5\u05d8 \u05dc\u05d0 \u05e7\u05d5\u05e8\u05d9\u05dd, \u05d1\u05dc\u05d9 \u05e9\u05d5\u05dd \u05d4\u05d5\u05d3\u05e2\u05ea \u05e9\u05d2\u05d9\u05d0\u05d4.' +
        '\n   \u05ea\u05d9\u05e7\u05d5\u05df: \u05e2\u05d5\u05e8\u05da \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 \u05d1\u05d7\u05e8 setupTriggers \u05d1\u05ea\u05e4\u05e8\u05d9\u05d8 \u05d4\u05e4\u05d5\u05e0\u05e7\u05e6\u05d9\u05d5\u05ea \u2192 \u05d4\u05e8\u05e5 \u25b6');
    } else {
      passed.push('\u05db\u05dc \u05d4\u05d8\u05e8\u05d9\u05d2\u05e8\u05d9\u05dd \u05de\u05d5\u05ea\u05e7\u05e0\u05d9\u05dd (' + EXPECTED_TRIGGERS.length + ')');
    }
  } catch (err) {
    problems.push('\u05d1\u05d3\u05d9\u05e7\u05ea \u05d4\u05d8\u05e8\u05d9\u05d2\u05e8\u05d9\u05dd \u05e0\u05db\u05e9\u05dc\u05d4: ' + err);
  }

  // 2. Did the last scrape run actually SUCCEED? The most direct signal there
  //    is - a cancelled or failed run is known within minutes, instead of
  //    waiting for the data to age past a threshold.
  try {
    const run = lastScrapeRun_();
    if (run && run.conclusion !== 'success') {
      problems.push('\u05d4\u05e8\u05d9\u05e6\u05d4 \u05d4\u05d0\u05d7\u05e8\u05d5\u05e0\u05d4 \u05e9\u05dc \u05e1\u05e7\u05e8\u05d9\u05d9\u05e4\u05e8 \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd \u05d4\u05e1\u05ea\u05d9\u05d9\u05de\u05d4 \u05d1-' + run.conclusion +
        ' (' + run.created_at + ')' +
        '\n   ' + run.html_url +
        '\n   \u05db\u05dc\u05d5\u05de\u05e8 \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd \u05d1\u05d8\u05d1\u05dc\u05d4 \u05dc\u05d0 \u05d4\u05ea\u05e2\u05d3\u05db\u05e0\u05d5 \u05de\u05d0\u05d6. \u05d0\u05dd \u05d6\u05d4 cancelled \u2014 \u05d4\u05e8\u05d9\u05e6\u05d4' +
        '\n   \u05e0\u05d7\u05ea\u05db\u05d4 \u05e2\u05dc \u05de\u05d2\u05d1\u05dc\u05ea \u05d4\u05d6\u05de\u05df \u05e9\u05dc \u05d4-workflow.');
    } else if (run) {
      passed.push('\u05e8\u05d9\u05e6\u05ea \u05d4\u05e1\u05e7\u05e8\u05d9\u05d9\u05e4\u05e8 \u05d4\u05d0\u05d7\u05e8\u05d5\u05e0\u05d4: success (' + run.created_at + ')');
    }
  } catch (err) {
    // Not fatal to the other checks, but it cannot count as a pass either -
    // an unreachable check is an unknown, and unknowns belong in the alert.
    problems.push('\u05dc\u05d0 \u05e0\u05d9\u05ea\u05df \u05dc\u05d1\u05d3\u05d5\u05e7 \u05d0\u05ea \u05e8\u05d9\u05e6\u05ea \u05d4\u05e1\u05e7\u05e8\u05d9\u05d9\u05e4\u05e8 \u05de\u05d5\u05dc GitHub: ' + err +
      '\n   \u05db\u05dc\u05d5\u05de\u05e8 \u05d0\u05d9\u05df \u05dc\u05d9 \u05d0\u05d9\u05e9\u05d5\u05e8 \u05e9\u05d4\u05e1\u05e8\u05d9\u05e7\u05d4 \u05d4\u05d0\u05d7\u05e8\u05d5\u05e0\u05d4 \u05d4\u05e6\u05dc\u05d9\u05d7\u05d4.');
  }

  // 3. Upstream end-to-end: did fresh prices actually LAND in the sheet?
  //    If the scraper stopped writing, the sheet quietly ages, every sync
  //    "succeeds" with stale numbers, and the purchase bot compares esim.dog
  //    against yesterday's prices.
  try {
    const f = scraperFreshness_();
    if (f.error) {
      problems.push('\u05d1\u05d3\u05d9\u05e7\u05ea \u05e8\u05e2\u05e0\u05e0\u05d5\u05ea \u05d4\u05d8\u05d1\u05dc\u05d4 \u05e0\u05db\u05e9\u05dc\u05d4: ' + f.error);
    } else if (f.stale || f.blank) {
      problems.push('\u05d1\u05d8\u05d1\u05dc\u05ea \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd ' + (f.stale + f.blank) + ' \u05de\u05ea\u05d5\u05da ' + f.total +
        ' \u05e9\u05d5\u05e8\u05d5\u05ea \u05dc\u05d0 \u05e2\u05d5\u05d3\u05db\u05e0\u05d5 \u05d1\u05d9\u05d5\u05ea\u05e8 \u05de-' + MAX_SCRAPE_STALE_HOURS + ' \u05e9\u05e2\u05d5\u05ea' +
        (f.blank ? ' (' + f.blank + ' \u05de\u05d4\u05df \u05d1\u05dc\u05d9 \u05d7\u05d5\u05ea\u05de\u05ea \u05d1\u05db\u05dc\u05dc)' : '') +
        '.\n   \u05d4\u05e2\u05d3\u05db\u05d5\u05df \u05d4\u05d9\u05e9\u05df \u05d1\u05d9\u05d5\u05ea\u05e8: ' + (f.oldest ? f.oldest.toISOString() : '\u05dc\u05d0 \u05d9\u05d3\u05d5\u05e2') +
        '\n   \u05d4\u05d1\u05d5\u05d8\u05d9\u05dd \u05e2\u05d5\u05d1\u05d3\u05d9\u05dd \u05dc\u05e4\u05d9 \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd \u05d4\u05d0\u05dc\u05d4 \u2014 \u05d1\u05d3\u05d5\u05e7 \u05d0\u05ea esim-price-scraper \u2192 Actions.');
    } else {
      passed.push('\u05db\u05dc ' + f.total + ' \u05e9\u05d5\u05e8\u05d5\u05ea \u05d4\u05de\u05d7\u05d9\u05e8\u05d9\u05dd \u05e2\u05d5\u05d3\u05db\u05e0\u05d5 \u05d1-' +
        MAX_SCRAPE_STALE_HOURS + ' \u05d4\u05e9\u05e2\u05d5\u05ea \u05d4\u05d0\u05d7\u05e8\u05d5\u05e0\u05d5\u05ea');
    }
  } catch (err) {
    problems.push('\u05d1\u05d3\u05d9\u05e7\u05ea \u05e8\u05e2\u05e0\u05e0\u05d5\u05ea \u05d4\u05d8\u05d1\u05dc\u05d4 \u05e0\u05db\u05e9\u05dc\u05d4: ' + err);
  }

  // 4. Downstream: does the live site actually serve fresh data?
  try {
    const res = UrlFetchApp.fetch(OVERLAY_URL + '?cb=' + Date.now(),
      { muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) {
      problems.push('\u05d4\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05de\u05d7\u05d6\u05d9\u05e8 \u05d0\u05ea \u05e7\u05d5\u05d1\u05e5 \u05d4\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd: HTTP ' +
        res.getResponseCode() + ' \u05de-' + OVERLAY_URL);
    } else {
      const updated = new Date(JSON.parse(res.getContentText()).updated);
      const hours = (Date.now() - updated.getTime()) / 36e5;
      Logger.log('site data age: ' + hours.toFixed(1) + 'h');
      // The overlay `updated` only moves when a price actually CHANGED (the
      // endpoint skips no-op commits). A successful recent sync is just as
      // fresh - the site provably has today's numbers, they're identical.
      const lastOk = PropertiesService.getScriptProperties().getProperty('LAST_SYNC_OK');
      const okHours = lastOk ? (Date.now() - new Date(lastOk).getTime()) / 36e5 : Infinity;
      if (hours < MAX_STALE_HOURS) {
        passed.push('\u05e0\u05ea\u05d5\u05e0\u05d9 \u05d4\u05d0\u05ea\u05e8 \u05e2\u05d5\u05d3\u05db\u05e0\u05d5 \u05dc\u05e4\u05e0\u05d9 ' + hours.toFixed(1) + ' \u05e9\u05e2\u05d5\u05ea');
      } else if (okHours < MAX_STALE_HOURS) {
        passed.push('\u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05d0\u05d7\u05e8\u05d5\u05df \u05e8\u05e5 \u05d1\u05d4\u05e6\u05dc\u05d7\u05d4 \u05dc\u05e4\u05e0\u05d9 ' + okHours.toFixed(1) +
          ' \u05e9\u05e2\u05d5\u05ea \u05d5\u05dc\u05d0 \u05de\u05e6\u05d0 \u05e9\u05d9\u05e0\u05d5\u05d9\u05d9 \u05de\u05d7\u05d9\u05e8\u05d9\u05dd (\u05d5\u05dc\u05db\u05df \u05d7\u05d5\u05ea\u05de\u05ea \u05d4\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05d6\u05d6\u05d4 \u2014 \u05d6\u05d4 \u05ea\u05e7\u05d9\u05df)');
      } else {
        problems.push('\u05d4\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05d1\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05d4\u05ea\u05e2\u05d3\u05db\u05e0\u05d5 ' + Math.round(hours) + ' \u05e9\u05e2\u05d5\u05ea' +
          '\n   \u05d4\u05e2\u05d3\u05db\u05d5\u05df \u05d4\u05d0\u05d7\u05e8\u05d5\u05df \u05d1\u05d0\u05ea\u05e8: ' + updated.toISOString() +
          '\n   \u05d5\u05d2\u05dd \u05dc\u05d0 \u05d4\u05d9\u05d4 \u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05de\u05d5\u05e6\u05dc\u05d7 \u05d1-' + MAX_STALE_HOURS + ' \u05d4\u05e9\u05e2\u05d5\u05ea \u05d4\u05d0\u05d7\u05e8\u05d5\u05e0\u05d5\u05ea.' +
          '\n   \u05dc\u05ea\u05d9\u05e7\u05d5\u05df \u05de\u05d9\u05d9\u05d3\u05d9: \u05dc\u05d4\u05e8\u05d9\u05e5 fullSync \u05de\u05e2\u05d5\u05e8\u05da \u05d4-Apps Script.');
      }
    }
  } catch (err) {
    problems.push('\u05d1\u05d3\u05d9\u05e7\u05ea \u05d4\u05d0\u05ea\u05e8 \u05e0\u05db\u05e9\u05dc\u05d4: ' + err);
  }

  // 5. One verdict, one email.
  const passedText = passed.length
    ? '\n\n\u05de\u05d4 \u05db\u05df \u05e0\u05d1\u05d3\u05e7 \u05d5\u05e2\u05d1\u05e8:\n\u2022 ' + passed.join('\n\u2022 ') : '';
  if (problems.length) {
    alert_('\u05d4\u05d1\u05d3\u05d9\u05e7\u05d4 \u05d4\u05d9\u05d5\u05de\u05d9\u05ea \u05de\u05e6\u05d0\u05d4 ' + problems.length + ' \u05ea\u05e7\u05dc\u05d5\u05ea',
      problems.map(function (p, i) { return (i + 1) + '. ' + p; }).join('\n\n') + passedText);
  } else {
    // Daily all-clear so a quiet inbox is proof it ran, not that it broke.
    report_('\u05d4\u05d1\u05d3\u05d9\u05e7\u05d4 \u05d4\u05d9\u05d5\u05de\u05d9\u05ea \u05e2\u05d1\u05e8\u05d4 \u2014 \u05d4\u05db\u05dc \u05ea\u05e7\u05d9\u05df \u2713',
      '\u05db\u05dc \u05d4\u05d1\u05d3\u05d9\u05e7\u05d5\u05ea \u05e2\u05d1\u05e8\u05d5:\n\u2022 ' + passed.join('\n\u2022 '));
  }
}

// -- coupon tab -> the site's coupon store ---------------------------
// Discount codes used to be a table inside the site's own JavaScript, which
// meant anyone reading the page could read the codes. They now live in the
// site's key-value store and are edited HERE: this tab is what the owner sees,
// and this block is what makes the till agree with it.
//
// Two writers push the same three grey columns: this script (instantly on an
// edit, plus every 30 minutes) and coupons-sync.yml in the scraper repo (hourly,
// and the one that still runs when this project is out of quota). Both write
// the same derived values from the same server answer, so an overlap costs a
// duplicate request and nothing else - only the append of a code the sheet does
// not have yet could race, and a script lock keeps this side of it single-file.
const COUPON_TAB = '\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd';
const COUPON_ORDERS_URL = 'https://www.waverole.com/api/orders';
const COUPON_HEADERS = [
  '\u05e7\u05d5\u05d3 - Code',
  '\u05d4\u05e0\u05d7\u05d4 % - Percent',
  '\u05d4\u05e0\u05d7\u05d4 $ - Fixed',
  '\u05ea\u05e7\u05e8\u05d4 $ - Max off',
  '\u05de-GB (\u05db\u05d5\u05dc\u05dc) - Min GB',
  '\u05e2\u05d3 GB (\u05db\u05d5\u05dc\u05dc) - Max GB',
  '\u05dc\u05d0 \u05ea\u05e7\u05e3 \u05dc - Exclude',
  '\u05ea\u05e7\u05e3 \u05e8\u05e7 \u05dc - Only',
  '\u05de\u05e7\u05e1\u05f3 \u05e9\u05d9\u05de\u05d5\u05e9\u05d9\u05dd - Max uses',
  '\u05dc\u05db\u05dc \u05dc\u05e7\u05d5\u05d7 - Per customer',
  '\u05de\u05ea\u05d0\u05e8\u05d9\u05da - Starts',
  '\u05e2\u05d3 \u05ea\u05d0\u05e8\u05d9\u05da - Expires',
  '\u05e4\u05e2\u05d9\u05dc - Active',
  '\u05e8\u05e7 \u05dc\u05d0\u05d9\u05de\u05d9\u05d9\u05dc - Email',
  '\u05ea\u05d5\u05d5\u05d9\u05ea - Label',
  '\u05d4\u05e2\u05e8\u05d4 - Note',
  '\u05e9\u05d9\u05de\u05d5\u05e9\u05d9\u05dd - Uses',
  '\u05de\u05e7\u05d5\u05e8 - Source',
  '\u05e1\u05d5\u05e0\u05db\u05e8\u05df - Synced',
];
const COUPON_SOURCE_HE = {
  builtin: '\u05de\u05d5\u05d1\u05e0\u05d4',
  sheet: '\u05d2\u05d9\u05dc\u05d9\u05d5\u05df',
  auto: '\u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9',
  api: 'API',
};
const COUPON_TZ = 'Asia/Jerusalem';
const COUPON_CODE_RE = /^[A-Z0-9]{2,24}$/;
const COUPON_TRUE = ['\u05db\u05df', 'yes', 'y', 'true', '1', 'v', '\u2713', 'on', '\u05e4\u05e2\u05d9\u05dc'];

/** Trim + uppercase, or '' for anything the till would refuse anyway. */
function couponCode_(v) {
  const c = String(v === null || v === undefined ? '' : v).trim().toUpperCase();
  return COUPON_CODE_RE.test(c) ? c : '';
}

function couponNum_(v, field, lo, hi) {
  if (v === '' || v === null || v === undefined) return null;
  if (typeof v === 'boolean') throw new Error(field + ': expected a number');
  const n = typeof v === 'number' ? v
    : parseFloat(String(v).replace(/[$%,\u20aa\s]/g, ''));
  if (isNaN(n)) throw new Error(field + ': not a number');
  if ((lo !== null && n < lo) || (hi !== null && n > hi)) {
    throw new Error(field + ': ' + n + ' is out of range');
  }
  return n;
}

/**
 * Percent, with the percent-FORMAT trap defused: a cell formatted as a percent
 * reads back as 0.1, not 10. Nobody ships a 0.1% coupon, so anything under 1 is
 * the format talking. Same class of bug as the currency format that once
 * cancelled three paid orders.
 */
function couponPct_(v, field) {
  const n = couponNum_(v, field, 0, 100);
  return n !== null && n > 0 && n < 1 ? n * 100 : n;
}

function couponInt_(v, field) {
  const n = couponNum_(v, field, 0, null);
  if (n === null) return null;
  if (Math.abs(n - Math.round(n)) > 1e-9) {
    throw new Error(field + ': must be a whole number');
  }
  return Math.round(n);
}

/** A blank counts as NOT active - a half-typed row must not go live. */
function couponBool_(v) {
  if (typeof v === 'boolean') return v;
  const s = String(v === null || v === undefined ? '' : v).trim().toLowerCase();
  return COUPON_TRUE.indexOf(s) >= 0;
}

function couponList_(v) {
  if (typeof v === 'number') return [String(v)];
  return String(v === null || v === undefined ? '' : v)
    .split(/[,\n;]+/).map(s => s.trim()).filter(Boolean);
}

/**
 * Sheet date -> an instant, or null.
 *
 * A start opens at 00:00 Israel and an expiry runs to 23:59:59 Israel. A bare
 * 'yyyy-MM-dd' is read by the site as UTC midnight, which retires a coupon
 * three hours before the date printed beside it. The offset is taken at NOON of
 * that day, the one hour a daylight-saving change can never land on.
 */
function couponIso_(v, endOfDay) {
  if (v === '' || v === null || v === undefined) return null;
  let d;
  if (Object.prototype.toString.call(v) === '[object Date]') {
    d = v;
  } else {
    const s = String(v).trim();
    if (!s) return null;
    if (s.indexOf('T') > 0) return s;              // already an instant
    let m = s.match(/^(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})$/);
    if (m) {
      d = new Date(+m[1], +m[2] - 1, +m[3], 12);
    } else {
      m = s.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})$/);
      if (!m) throw new Error('date: "' + s + '" is not a date');
      let y = +m[3];
      if (y < 100) y += 2000;
      d = new Date(y, +m[2] - 1, +m[1], 12);       // day first: an Israeli sheet
    }
  }
  const day = Utilities.formatDate(d, COUPON_TZ, 'yyyy-MM-dd');
  const off = Utilities.formatDate(new Date(day + 'T12:00:00Z'), COUPON_TZ, 'XXX');
  return day + (endOfDay ? 'T23:59:59' : 'T00:00:00') + off;
}

/** One sheet row -> one upsert input, or null when the row carries no code. */
function couponRowInput_(cells, idx) {
  const at = i => cells[idx[COUPON_HEADERS[i]]];
  const code = couponCode_(at(0));
  if (!code) {
    if (String(at(0) || '').trim()) {
      throw new Error('code: letters and digits only, 2-24 characters');
    }
    return null;
  }
  const email = String(at(13) || '').trim().toLowerCase();
  const label = String(at(14) || '').trim().slice(0, 40);
  // Rows the server wrote here are its own: a minted personal code pushed
  // back up would be reborn as a permanent sheet definition the day its KV
  // record expires. The source cell says so; the shape says so if the cell
  // was lost.
  if (String(at(17) || '').trim() === COUPON_SOURCE_HE.auto) return null;
  if (/^WR[A-Z2-9]{8}$/.test(code) && email) return null;
  return {
    code: code,
    pct: couponPct_(at(1), COUPON_HEADERS[1]),
    fixed_usd: couponNum_(at(2), COUPON_HEADERS[2], 0, null),
    max_off_usd: couponNum_(at(3), COUPON_HEADERS[3], 0, null),
    min_gb: couponNum_(at(4), COUPON_HEADERS[4], 0, null),
    max_gb: couponNum_(at(5), COUPON_HEADERS[5], 0, null),
    exclude: couponList_(at(6)),
    only: couponList_(at(7)),
    max_uses: couponInt_(at(8), COUPON_HEADERS[8]),
    per_customer: couponInt_(at(9), COUPON_HEADERS[9]),
    starts_at: couponIso_(at(10), false),
    expires_at: couponIso_(at(11), true),
    active: couponBool_(at(12)),
    bound_email: email || null,
    label: label || null,
    note: String(at(15) || '').trim() || null,
  };
}

/**
 * The site's token, or null after one email a day.
 *
 * Without it this whole feature does nothing and looks exactly like a broken
 * sheet - the same silence that once hid the usage meter being dead. One email,
 * at most once a day, then quiet. Its own property so a receipts warning and a
 * coupon warning never mask each other.
 */
function couponToken_() {
  const props = PropertiesService.getScriptProperties();
  const tok = props.getProperty('ORDERS_TOKEN');
  if (tok) return tok;
  const today = Utilities.formatDate(new Date(), COUPON_TZ, 'yyyy-MM-dd');
  if (props.getProperty('COUPON_TOKEN_WARNED_ON') !== today) {
    props.setProperty('COUPON_TOKEN_WARNED_ON', today);
    alert_('\u05d7\u05e1\u05e8 ORDERS_TOKEN \u2014 \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd \u05dc\u05d0 \u05de\u05e1\u05ea\u05e0\u05db\u05e8\u05e0\u05d9\u05dd',
      '\u05e2\u05e8\u05db\u05ea \u05d0\u05ea \u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd, \u05d0\u05d1\u05dc \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05dc\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05e8\u05e5 \u05db\u05d9 \u05d0\u05d9\u05df ORDERS_TOKEN \u05d1\u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8.\n\n' +
      '\u05ea\u05d9\u05e7\u05d5\u05df: \u05e2\u05d5\u05e8\u05da \u05d4\u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 \u2699\ufe0f \u05d4\u05d2\u05d3\u05e8\u05d5\u05ea \u05d4\u05e4\u05e8\u05d5\u05d9\u05e7\u05d8 \u2192 \u05de\u05d0\u05e4\u05d9\u05d9\u05e0\u05d9 \u05e1\u05e7\u05e8\u05d9\u05e4\u05d8 \u2192 ' +
      '\u05d4\u05d5\u05e1\u05e4\u05ea \u05de\u05d0\u05e4\u05d9\u05d9\u05df \u2192 \u05e9\u05dd: ORDERS_TOKEN \u2192 \u05d4\u05d3\u05d1\u05e7 \u05d0\u05ea \u05d4\u05e2\u05e8\u05da \u2192 \u05e9\u05de\u05d9\u05e8\u05d4.\n' +
      '\u05d0\u05d7\u05e8 \u05db\u05da \u05d4\u05e8\u05e5 syncCouponsNow \u05db\u05d3\u05d9 \u05dc\u05d5\u05d5\u05d3\u05d0 \u05e9\u05d4\u05db\u05dc \u05e2\u05d5\u05d1\u05d3.');
  }
  return null;
}

/** POST when there is a payload, GET ?coupons=1 when there is not. */
function couponCall_(tok, payload) {
  const url = COUPON_ORDERS_URL + (payload ? '' : '?coupons=1');
  const opts = {
    method: payload ? 'post' : 'get',
    headers: { Authorization: 'Bearer ' + tok },
    muteHttpExceptions: true,
  };
  if (payload) {
    opts.contentType = 'application/json';
    opts.payload = JSON.stringify(payload);
  }
  const res = UrlFetchApp.fetch(url, opts);
  const code = res.getResponseCode();
  const body = res.getContentText();
  if (code !== 200) {
    alert_('\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd \u05e0\u05db\u05e9\u05dc (HTTP ' + code + ')',
      '\u05d4\u05e7\u05e8\u05d9\u05d0\u05d4 \u05dc-' + url + ' \u05d4\u05d7\u05d6\u05d9\u05e8\u05d4 ' + code + ':\n' + body.slice(0, 500));
    return null;
  }
  try {
    return JSON.parse(body);
  } catch (err) {
    alert_('\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd \u05d4\u05d7\u05d6\u05d9\u05e8 \u05ea\u05e9\u05d5\u05d1\u05d4 \u05dc\u05d0 \u05ea\u05e7\u05d9\u05e0\u05d4',
      '\u05d4\u05e7\u05e8\u05d9\u05d0\u05d4 \u05dc-' + url + ' \u05d4\u05e6\u05dc\u05d9\u05d7\u05d4 \u05d0\u05d1\u05dc \u05dc\u05d0 \u05d4\u05d7\u05d6\u05d9\u05e8\u05d4 JSON:\n' + body.slice(0, 500));
    return null;
  }
}

/**
 * The server's answer back into the sheet: uses/source/synced beside every row
 * it recognises, and a new row for every code it holds that the sheet does not -
 * the personal codes the survey reward mints. Written with setValues, never a
 * cell at a time.
 */
function couponWriteBack_(tab, values, idx, server, stamp) {
  const byCode = {};
  server.forEach(function (d) {
    const c = couponCode_(d.code);
    if (c) byCode[c] = d;
  });
  const ccol = idx[COUPON_HEADERS[0]];
  const qcol = idx[COUPON_HEADERS[16]];
  const rcol = idx[COUPON_HEADERS[17]];
  const scol = idx[COUPON_HEADERS[18]];

  const trio = [], seen = {};
  for (let r = 1; r < values.length; r++) {
    const code = couponCode_(values[r][ccol]);
    const d = code ? byCode[code] : null;
    // Not on the server: no count, no stamp - but the source cell stays, so
    // an expired personal code is still recognised as the server's.
    if (!d) { trio.push(['', values[r][rcol] || '', '']); continue; }
    seen[code] = true;
    trio.push([d.uses || 0, COUPON_SOURCE_HE[d.source] || d.source || '', stamp]);
  }
  if (trio.length) {
    if (rcol === qcol + 1 && scol === qcol + 2) {
      tab.getRange(2, qcol + 1, trio.length, 3).setValues(trio);
    } else {
      // A reordered sheet is still worth syncing; it just costs three writes.
      [qcol, rcol, scol].forEach(function (col, n) {
        tab.getRange(2, col + 1, trio.length, 1)
          .setValues(trio.map(t => [t[n]]));
      });
    }
  }

  const extra = [], names = [];
  Object.keys(byCode).sort().forEach(function (code) {
    if (seen[code]) return;
    const d = byCode[code];
    const row = [];
    for (let i = 0; i < COUPON_HEADERS.length; i++) row.push('');
    row[0] = code;
    row[1] = d.pct || '';
    row[2] = d.fixed_usd || '';
    row[3] = d.max_off_usd || '';
    row[4] = d.min_gb || '';
    row[5] = d.max_gb || '';
    row[6] = (d.exclude || []).join(', ');
    row[7] = (d.only || []).join(', ');
    row[8] = d.max_uses || '';
    row[9] = d.per_customer || '';
    row[10] = String(d.starts_at || '').slice(0, 10);
    row[11] = String(d.expires_at || '').slice(0, 10);
    row[12] = d.active ? '\u05db\u05df' : '\u05dc\u05d0';
    row[13] = d.bound_email || '';
    row[14] = d.label || '';
    row[15] = d.note || '';
    row[16] = d.uses || 0;
    row[17] = COUPON_SOURCE_HE[d.source] || d.source || '';
    row[18] = stamp;
    extra.push(row);
    // A personal code is a bearer secret. It belongs in the owner's sheet, not
    // in an execution log that gets pasted into a chat when something breaks.
    names.push(d.source === 'auto' || d.bound_email ? '(\u05d0\u05d9\u05e9\u05d9)' : code);
  });
  if (extra.length) {
    const first = values.length + 1;
    const need = first + extra.length - 1;
    if (need > tab.getMaxRows()) tab.insertRowsAfter(tab.getMaxRows(), need - tab.getMaxRows());
    tab.getRange(first, 1, extra.length, COUPON_HEADERS.length).setValues(extra);
  }
  return names;
}

// push: true from an edit (the tab changed), false from the timer (only the
// counters are wanted - re-sending an unchanged tab every half hour is three
// store commands a row against the budget the order queue lives on). An edit
// that could not take the lock leaves a flag, and the next timer run pushes
// for it; a dropped edit would otherwise sit in the sheet looking synced.
function syncCoupons_(push) {
  const props = PropertiesService.getScriptProperties();
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(60000);
  } catch (err) {
    if (push) props.setProperty('COUPON_SYNC_DUE', '1');
    return;
  }
  try {
    const tab = SpreadsheetApp.openById(SHEET_ID).getSheetByName(COUPON_TAB);
    if (!tab) return;
    const tok = couponToken_();
    if (!tok) return;
    if (!push && props.getProperty('COUPON_SYNC_DUE')) push = true;

    const values = tab.getDataRange().getValues();
    if (values.length < 1) return;
    const head = values[0].map(h => String(h).trim());
    const idx = {};
    COUPON_HEADERS.forEach(function (h) { idx[h] = head.indexOf(h); });
    const missing = COUPON_HEADERS.filter(h => idx[h] < 0);
    if (missing.length) {
      alert_('\u05e2\u05de\u05d5\u05d3\u05d4 \u05d7\u05e1\u05e8\u05d4 \u05d1\u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd \u2014 \u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05e0\u05e2\u05e6\u05e8',
        '\u05d4\u05e2\u05de\u05d5\u05d3\u05d5\u05ea \u05d4\u05d0\u05dc\u05d4 \u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d5 \u05d1\u05e9\u05d5\u05e8\u05ea \u05d4\u05db\u05d5\u05ea\u05e8\u05d5\u05ea:\n' + missing.join('\n') +
        '\n\n\u05d4\u05e1\u05e0\u05db\u05e8\u05d5\u05df \u05de\u05d5\u05e6\u05d0 \u05e2\u05de\u05d5\u05d3\u05d5\u05ea \u05dc\u05e4\u05d9 \u05d4\u05d8\u05e7\u05e1\u05d8 \u05e9\u05dc\u05d4\u05df, \u05d0\u05d6 \u05e9\u05d9\u05e0\u05d5\u05d9 \u05e9\u05dd \u05db\u05d5\u05ea\u05e8\u05ea \u05e2\u05d5\u05e6\u05e8 \u05d0\u05d5\u05ea\u05d5. ' +
        '\u05d9\u05e9 \u05dc\u05d4\u05d7\u05d6\u05d9\u05e8 \u05d0\u05ea \u05d4\u05e9\u05dd \u05d1\u05d3\u05d9\u05d5\u05e7 \u05db\u05e4\u05d9 \u05e9\u05d4\u05d5\u05d0 \u05db\u05d0\u05df, \u05d0\u05d5 \u05dc\u05e2\u05d3\u05db\u05df \u05d0\u05ea COUPON_HEADERS \u05d1\u05e7\u05d5\u05d3.');
      return;
    }

    const coupons = [], problems = [];
    for (let r = 1; r < values.length; r++) {
      try {
        const inp = couponRowInput_(values[r], idx);
        if (inp) coupons.push(inp);
      } catch (err) {
        problems.push('\u05e9\u05d5\u05e8\u05d4 ' + (r + 1) + ': ' + err.message);
      }
    }
    if (problems.length) {
      // One broken row must not stop the other twenty from reaching the till.
      alert_('\u05e9\u05d5\u05e8\u05d5\u05ea \u05e9\u05dc\u05d0 \u05e0\u05e7\u05e8\u05d0\u05d5 \u05d1\u05d8\u05d1\u05dc\u05ea \u05d4\u05e7\u05d5\u05e4\u05d5\u05e0\u05d9\u05dd (' + problems.length + ')',
        problems.join('\n') + '\n\n\u05e9\u05d0\u05e8 \u05d4\u05e9\u05d5\u05e8\u05d5\u05ea \u05e1\u05d5\u05e0\u05db\u05e8\u05e0\u05d5 \u05db\u05e8\u05d2\u05d9\u05dc.');
    }

    if (push) {
      if (!couponCall_(tok, { action: 'coupon_sync', coupons: coupons })) return;
      props.deleteProperty('COUPON_SYNC_DUE');
    }
    const state = couponCall_(tok, null);
    if (!state) return;

    const stamp = Utilities.formatDate(new Date(), COUPON_TZ, 'yyyy-MM-dd HH:mm');
    const added = couponWriteBack_(tab, values, idx, state.coupons || [], stamp);
    Logger.log('coupons: ' + (push ? 'pushed ' + coupons.length : 'pull only') + ', site holds ' +
      (state.coupons || []).length + ', appended ' + added.length +
      (added.length ? ' (' + added.join(', ') + ')' : '') +
      ', rewards awaiting approval: ' + (state.rewards_pending || []).length);
  } catch (err) {
    Logger.log('syncCoupons_ failed: ' + err);
  } finally {
    lock.releaseLock();
  }
}

/** The every-30-minute trigger: pull the use counters even on a quiet day. */
function pullCoupons() { syncCoupons_(false); }

/** Manual: function dropdown -> Run. Pushes the tab and pulls the counters. */
function syncCouponsNow() {
  syncCoupons_(true);
  Logger.log('done - see the lines above');
}
