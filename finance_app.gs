/**
 * Waverole — כספים.  Bot #7: the books, with a face.
 *
 * One file. Paste it into a new Apps Script project owned by the account that
 * owns the sheets (uper.request@gmail.com), run setup() once, then deploy it
 * as a web app. Everything else it builds itself.
 *
 * What setup() creates, in the OWNER's Drive so the owner keeps it:
 *   - the spreadsheet "Waverole — כספים" with all its tabs
 *   - a Drive folder "Waverole — קבלות" for photographed receipts
 *   - edit rights for the scraper service account, so the Python side can write
 *   - a daily trigger that reads this mailbox for receipts
 *
 * Why the work is split with finance_bot.py: a script can only read the
 * mailbox it runs as. This one runs as the owner and reads the owner's inbox
 * for overheads. The supplier's receipts land in waverolesupply@gmail.com, so
 * the Python bot reads those over IMAP and writes into the same spreadsheet.
 * Neither one can see the other's mailbox, and neither needs to.
 */

// ------------------------------------------------------------------ config

var SHEET_TITLE  = 'Waverole — כספים';
var FOLDER_TITLE = 'Waverole — קבלות';

// The account finance_bot.py authenticates as. It needs edit rights on the
// spreadsheet this script creates. (finance_bot.py --whoami prints it.)
var BOT_ACCOUNT = 'esim-scraper@esim-price-tracker-498508.iam.gserviceaccount.com';

var T_SUMMARY  = 'סקירה';
var T_SALES    = 'מכירות';
var T_EXPENSES = 'הוצאות';
var T_RECON    = 'התאמת ספק';
var T_MONTHLY  = 'חודשי';
var T_PACKAGES = 'רווח לפי חבילה';
var T_RATES    = 'שערי מטבע';

// Appended at the end, not inserted, so column positions already relied on
// elsewhere (api_reviewExpense hardcodes column 10 = סטטוס) stay correct.
var EXPENSE_HEADER = ['מזהה', 'תאריך', 'ספק', 'תיאור', 'קטגוריה', 'סכום',
                      'מטבע', 'סכום ש"ח', 'מקור', 'סטטוס', 'קבלה',
                      'מזהה מייל', 'הזמנה', 'הערות', 'סכום $'];

var CATEGORIES = ['עלות מכר', 'סליקה', 'תשתית', 'כלים', 'שיווק', 'עמלות',
                  'הקמה', 'אחר'];

var NEEDS_REVIEW = 'לבדיקה';
var CONFIRMED    = 'מאושר';

// Senders worth reading in the owner's own mailbox, and what they are.
var VENDOR_RULES = [
  ['esim.dog',        'ESIM.DOG',         'עלות מכר'],
  ['vercel',          'Vercel',           'תשתית'],
  ['upstash',         'Upstash',          'תשתית'],
  ['resend',          'Resend',           'תשתית'],
  ['cloudflare',      'Cloudflare',       'תשתית'],
  ['namecheap',       'Namecheap',        'תשתית'],
  ['godaddy',         'GoDaddy',          'תשתית'],
  ['google cloud',    'Google Cloud',     'תשתית'],
  ['google workspace','Google Workspace', 'תשתית'],
  ['github',          'GitHub',           'תשתית'],
  ['railway',         'Railway',          'תשתית'],
  ['supabase',        'Supabase',         'תשתית'],
  /* AI tooling is the single largest recurring cost here, so it gets its
     own category instead of disappearing into "infrastructure". */
  ['anthropic',       'Anthropic',        'כלים'],
  ['claude.ai',       'Anthropic',        'כלים'],
  ['openai',          'OpenAI',           'כלים'],
  ['cursor',          'Cursor',           'כלים'],
  ['midjourney',      'Midjourney',       'כלים'],
  ['canva',           'Canva',            'כלים'],
  ['figma',           'Figma',            'כלים'],
  ['google ads',      'Google Ads',       'שיווק'],
  ['googleadwords',   'Google Ads',       'שיווק'],
  ['facebookmail',    'Meta Ads',         'שיווק'],
  ['meta platforms',  'Meta Ads',         'שיווק'],
  ['tiktok',          'TikTok Ads',       'שיווק'],
  ['icount',          'iCount',           'סליקה'],
  ['stripe',          'Stripe',           'סליקה'],
  ['paypal',          'PayPal',           'סליקה']
];

function props_() { return PropertiesService.getScriptProperties(); }

// -------------------------------------------------------------------- setup

/**
 * Run this once from the editor. Safe to run again: it adopts whatever
 * already exists instead of making a second copy.
 */
function setup() {
  var ss = openBook_(true);
  var folder = openFolder_(true);

  ensureTabs_(ss);
  shareWithBot_(ss);

  installTrigger_('dailyScan', 3);

  var url = ss.getUrl();
  Logger.log('גיליון:  ' + url);
  Logger.log('תיקייה:  ' + folder.getUrl());
  Logger.log('שותף עם: ' + BOT_ACCOUNT);
  Logger.log('');
  Logger.log('עכשיו: Deploy → New deployment → Web app,');
  Logger.log('Execute as: Me, Who has access: Only myself.');
  return url;
}

function openBook_(create) {
  var id = props_().getProperty('SHEET_ID');
  if (id) {
    try { return SpreadsheetApp.openById(id); } catch (e) { /* recreate below */ }
  }
  var hits = DriveApp.getFilesByName(SHEET_TITLE);
  if (hits.hasNext()) {
    var ss = SpreadsheetApp.open(hits.next());
    props_().setProperty('SHEET_ID', ss.getId());
    return ss;
  }
  if (!create) throw new Error('הגיליון עדיין לא נוצר — יש להריץ setup()');
  var made = SpreadsheetApp.create(SHEET_TITLE);
  props_().setProperty('SHEET_ID', made.getId());
  return made;
}

function openFolder_(create) {
  var id = props_().getProperty('FOLDER_ID');
  if (id) {
    try { return DriveApp.getFolderById(id); } catch (e) { /* recreate below */ }
  }
  var hits = DriveApp.getFoldersByName(FOLDER_TITLE);
  var folder = hits.hasNext() ? hits.next()
             : (create ? DriveApp.createFolder(FOLDER_TITLE) : null);
  if (!folder) throw new Error('תיקיית הקבלות לא נמצאה');
  props_().setProperty('FOLDER_ID', folder.getId());
  return folder;
}

function ensureTabs_(ss) {
  var wanted = [T_SUMMARY, T_SALES, T_EXPENSES, T_RECON, T_MONTHLY,
                T_PACKAGES, T_RATES];
  wanted.forEach(function (name) {
    if (!ss.getSheetByName(name)) ss.insertSheet(name);
  });
  var exp = ss.getSheetByName(T_EXPENSES);
  if (exp.getLastRow() === 0) {
    exp.getRange(1, 1, 1, EXPENSE_HEADER.length)
       .setValues([EXPENSE_HEADER]).setFontWeight('bold');
    exp.setFrozenRows(1);
  }
  var first = ss.getSheets()[0];
  if (first.getName() === 'Sheet1' || first.getName() === 'גיליון1') {
    if (first.getLastRow() === 0) ss.deleteSheet(first);
  }
  wanted.forEach(function (n) {
    var sh = ss.getSheetByName(n);
    if (sh) sh.setRightToLeft(true);
  });
}

function shareWithBot_(ss) {
  if (!BOT_ACCOUNT || BOT_ACCOUNT.indexOf('@') < 0) return;
  try {
    DriveApp.getFileById(ss.getId()).addEditor(BOT_ACCOUNT);
  } catch (e) {
    Logger.log('לא הצלחתי לשתף עם ' + BOT_ACCOUNT + ': ' + e);
  }
}

function installTrigger_(handler, hour) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === handler) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger(handler).timeBased().atHour(hour).everyDays(1).create();
}

// ------------------------------------------------------- mailbox ingestion

/**
 * Daily: read THIS mailbox for anything that looks like a business receipt.
 *
 * Conservative on purpose. A human inbox is mostly not receipts, and a
 * marketing email that happens to say "$9.99" must never become an expense.
 * A message has to come from a sender we recognise, read like a receipt, and
 * produce a total. Anything short of that is left alone.
 */
function dailyScan() {
  var added = scanMailbox_(120);
  Logger.log('נוספו ' + added + ' הוצאות מהמייל');
}

/**
 * Run ONCE, by hand, after setup(): sweeps three years back so the history
 * that predates this system lands in the books. The daily scan only looks
 * 120 days back, which would silently leave older subscriptions out.
 *
 * Safe to run more than once — rows are keyed by Gmail message id, so a
 * second pass adds nothing. If it stops early (too many results for one
 * six-minute execution), just run it again; it resumes where it left off.
 */
function backfill() {
  var added = scanMailbox_(1095);
  Logger.log('BACKFILL: נוספו ' + added + ' הוצאות. ' +
             'אם המספר גדול — הרץ שוב כדי להשלים את השאר.');
  return added;
}

function scanMailbox_(days) {
  var ss = openBook_(false);
  var sheet = ss.getSheetByName(T_EXPENSES);
  var seen = existingKeys_(sheet);

  /* Subject words alone miss a lot: Hebrew suppliers write "אישור הזמנה",
     Apple writes "Your receipt from Apple", and some send the amount with
     no keyword at all. So known billing senders are searched too, and the
     parser decides what is really a receipt. */
  var subjectTerms = ['receipt', 'invoice', 'billing', 'payment', 'purchase',
    'subscription', 'order', 'חשבונית', 'קבלה', 'תשלום', 'חיוב', 'הזמנה',
    'מנוי', 'הקמה'];
  var senders = ['stripe.com', 'anthropic.com', 'claude.ai', 'openai.com',
    'vercel.com', 'cloudflare.com', 'namecheap.com', 'godaddy.com',
    'github.com', 'google.com', 'paypal.com', 'apple.com', 'railway.app',
    'upstash.com', 'resend.com', 'cursor.com', 'icount.co.il'];

  var query = 'newer_than:' + days + 'd -in:chats -in:drafts (' +
    subjectTerms.map(function (w) { return 'subject:' + w; }).join(' OR ') +
    ' OR from:(' + senders.join(' OR ') + '))';

  var rows = [];
  var scanned = 0, start = 0, PAGE = 100, LIMIT = 1500, truncated = false;

  /* Paginated: a single search() call caps out, and a first backfill over
     two years is far more than one page. */
  while (start < LIMIT) {
    var threads = GmailApp.search(query, start, PAGE);
    if (!threads.length) break;
    for (var i = 0; i < threads.length; i++) {
      var msgs = threads[i].getMessages();
      for (var j = 0; j < msgs.length; j++) {
        scanned++;
        var parsed = parseReceiptMessage_(msgs[j]);
        if (!parsed) continue;
        if (seen[parsed.key]) continue;   /* message id: re-runs are safe */
        seen[parsed.key] = true;
        rows.push(expenseRow_(parsed));
      }
    }
    start += threads.length;
    if (threads.length < PAGE) break;
    /* Apps Script kills a run at six minutes. Stop early and let the next
       run continue rather than dying half-written. */
    if (rows.length > 400) { truncated = true; break; }
  }
  Logger.log('scan: ' + scanned + ' messages, ' + rows.length + ' new' +
             (truncated ? ' (stopped early — run again for the rest)' : ''));

  if (rows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length,
                   EXPENSE_HEADER.length).setValues(rows);
  }
  return rows.length;
}

function parseReceiptMessage_(msg) {
  var from = String(msg.getFrom() || '');
  var subject = String(msg.getSubject() || '');
  var body = '';
  try { body = String(msg.getPlainBody() || ''); } catch (e) { body = ''; }

  var vendor = vendorOf_(from + ' ' + subject);
  if (!vendor) return null;

  var hay = subject + '\n' + body.slice(0, 4000);
  if (!/receipt|invoice|payment|billed|your bill|חשבונית|קבלה|חיוב|תשלום/i.test(hay)) {
    return null;
  }

  var money = findTotal_(hay);
  if (!money) return null;

  var when = msg.getDate();
  return {
    key: 'gm-' + msg.getId(),
    when: when,
    vendor: vendor.name,
    description: subject,
    category: vendor.category,
    amount: money.amount,
    currency: money.currency,
    source: 'מייל',
    status: CONFIRMED,
    receipt: msg.getThread().getPermalink(),
    messageId: msg.getId(),
    note: ''
  };
}

function vendorOf_(hay) {
  var low = String(hay).toLowerCase();
  for (var i = 0; i < VENDOR_RULES.length; i++) {
    if (low.indexOf(VENDOR_RULES[i][0]) >= 0) {
      return { name: VENDOR_RULES[i][1], category: VENDOR_RULES[i][2] };
    }
  }
  return null;
}

/** Pull the payable total out of a receipt body. */
function findTotal_(text) {
  var re = new RegExp(
    '(?:amount\\s+paid|total|amount\\s+due|grand\\s+total|סה"?כ|לתשלום|סכום)' +
    '\\s*[:\\-]?\\s*([$₪€£]?\\s*[0-9][0-9,]*(?:\\.[0-9]+)?\\s*' +
    '(?:USD|ILS|NIS|EUR|GBP|\\$|₪|€|£)?)', 'i');
  var m = re.exec(text);
  if (!m) return null;
  return parseMoney_(m[1]);
}

function parseMoney_(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  var s = String(raw);
  var m = /[0-9][0-9,]*(?:\.[0-9]+)?/.exec(s);
  if (!m) return null;
  var amount = parseFloat(m[0].replace(/,/g, ''));
  if (isNaN(amount)) return null;
  var low = s.toLowerCase();
  var currency = 'ILS';
  if (low.indexOf('$') >= 0 || low.indexOf('usd') >= 0) currency = 'USD';
  else if (low.indexOf('₪') >= 0 || low.indexOf('ils') >= 0 ||
           low.indexOf('nis') >= 0) currency = 'ILS';
  else if (low.indexOf('€') >= 0 || low.indexOf('eur') >= 0) currency = 'EUR';
  else if (low.indexOf('£') >= 0 || low.indexOf('gbp') >= 0) currency = 'GBP';
  return { amount: amount, currency: currency };
}

function existingKeys_(sheet) {
  var out = {};
  var last = sheet.getLastRow();
  if (last < 2) return out;
  sheet.getRange(2, 1, last - 1, 1).getValues().forEach(function (r) {
    if (r[0]) out[String(r[0])] = true;
  });
  return out;
}

function expenseRow_(e) {
  var rate = rateFor_(e.when);
  var ils = e.currency === 'ILS' ? e.amount
          : Math.round(e.amount * rate * 100) / 100;
  /* The book runs in USD (see finance-system memory, rule 1); ILS is only
     an accountant line. rate is ILS per USD, so ILS->USD is a division. */
  var usd = e.currency === 'USD' ? e.amount
          : Math.round(e.amount / rate * 100) / 100;
  return [e.key, formatWhen_(e.when), e.vendor, e.description, e.category,
          round2_(e.amount), e.currency, ils, e.source, e.status,
          e.receipt || '', e.messageId || '', e.orderId || '', e.note || '',
          usd];
}

function formatWhen_(d) {
  return Utilities.formatDate(d, 'Asia/Jerusalem', 'yyyy-MM-dd HH:mm');
}

function round2_(n) { return Math.round(Number(n) * 100) / 100; }

/**
 * USD -> ILS on a given day, from the rates the Python side has already
 * cached in the spreadsheet. Falls back to the newest rate on file, then to
 * a constant, because a missing rate must not stop an expense being saved.
 */
function rateFor_(when) {
  var cache = CacheService.getScriptCache();
  var key = 'rate-' + Utilities.formatDate(when, 'Asia/Jerusalem', 'yyyy-MM-dd');
  var hit = cache.get(key);
  if (hit) return parseFloat(hit);

  var rate = 3.0;
  try {
    var sh = openBook_(false).getSheetByName(T_RATES);
    if (sh && sh.getLastRow() > 1) {
      var values = sh.getRange(2, 1, sh.getLastRow() - 1, 2).getValues();
      var want = Utilities.formatDate(when, 'Asia/Jerusalem', 'yyyy-MM-dd');
      var best = null, newest = null;
      values.forEach(function (r) {
        var d = String(r[0]), v = parseFloat(r[1]);
        if (!d || isNaN(v)) return;
        if (d === want) best = v;
        if (!newest || d > newest.d) newest = { d: d, v: v };
      });
      rate = best || (newest ? newest.v : rate);
    }
  } catch (e) { /* keep the fallback */ }
  cache.put(key, String(rate), 21600);
  return rate;
}

// ------------------------------------------------------------- receipt photo

/**
 * A photographed receipt: store the image, then read it.
 *
 * Google Drive will OCR an image when it converts it to a Doc, which is the
 * only OCR available from Apps Script. It needs the advanced Drive service
 * switched on (Services → Drive API). Without it the photo is still filed
 * and the owner types the amount — the receipt is never lost either way.
 */
function api_uploadReceipt(payload) {
  var ss = openBook_(false);
  try {
    var bytes = Utilities.base64Decode(payload.data);
    var blob = Utilities.newBlob(bytes, payload.mime || 'image/jpeg',
                                 payload.name || 'receipt.jpg');
    var folder = openFolder_(true);
    var file = folder.createFile(blob);
    file.setDescription('נסרק ' + formatWhen_(new Date()));

    var text = '';
    var ocrFailed = '';
    try {
      text = ocrText_(blob, folder);
    } catch (e) {
      ocrFailed = String(e);
    }

    var guess = readReceiptText_(text);

    /* The client's toast tells the owner to approve this in the "הוצאות"
       tab — so the row has to actually exist there. Land it as לבדיקה even
       when OCR could not read an amount: an unreadable receipt is still a
       receipt, and the owner needs to see it to fix it by hand. */
    var row = expenseRow_({
      key: 'photo-' + Utilities.getUuid().slice(0, 12),
      when: guess.date ? new Date(guess.date + 'T12:00:00') : new Date(),
      vendor: guess.vendor || 'לא זוהה',
      description: '',
      category: guess.category || 'אחר',
      amount: guess.amount || 0,
      currency: guess.currency || 'ILS',
      source: 'צילום',
      status: NEEDS_REVIEW,
      receipt: file.getUrl(),
      note: ocrFailed ? 'קריאת הקבלה נכשלה — יש להשלים ידנית' : ''
    });
    ss.getSheetByName(T_EXPENSES).appendRow(row);

    var out = apiDataFor_(ss);
    out.url = file.getUrl();
    out.fileId = file.getId();
    out.text = text.slice(0, 1200);
    out.ocrAvailable = !ocrFailed;
    out.ocrError = ocrFailed;
    out.guess = guess;
    return out;
  } catch (e) {
    return fail_(ss, String(e));
  }
}

function ocrText_(blob, folder) {
  if (typeof Drive === 'undefined' || !Drive.Files) {
    throw new Error('שירות Drive המתקדם לא מופעל');
  }
  var temp = Drive.Files.create(
    { name: 'ocr-' + Date.now(), mimeType: MimeType.GOOGLE_DOCS,
      parents: [folder.getId()] },
    blob);
  var id = temp.id || temp.getId();
  var text = '';
  try {
    text = DocumentApp.openById(id).getBody().getText();
  } finally {
    try { DriveApp.getFileById(id).setTrashed(true); } catch (e2) { /* ignore */ }
  }
  return text;
}

/** Best-effort reading of a scanned receipt: total, date, vendor, category. */
function readReceiptText_(text) {
  var out = { amount: null, currency: 'ILS', vendor: '', category: 'אחר',
              date: '', confidence: 'low' };
  if (!text) return out;

  var money = findTotal_(text);
  if (!money) {
    // No labelled total. The largest number on a receipt is almost always
    // the amount payable, so offer it — flagged as a guess.
    var all = String(text).match(/[0-9][0-9,]*\.[0-9]{2}/g) || [];
    var best = 0;
    all.forEach(function (s) {
      var v = parseFloat(s.replace(/,/g, ''));
      if (!isNaN(v) && v > best) best = v;
    });
    if (best > 0) { money = { amount: best, currency: /\$/.test(text) ? 'USD' : 'ILS' }; }
  } else {
    out.confidence = 'high';
  }
  if (money) { out.amount = money.amount; out.currency = money.currency; }

  var vendor = vendorOf_(text);
  if (vendor) { out.vendor = vendor.name; out.category = vendor.category; }
  else {
    var line = String(text).split(/\r?\n/).filter(function (l) {
      return l.trim().length > 2;
    })[0];
    if (line) out.vendor = line.trim().slice(0, 40);
  }

  var d = /(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})/.exec(text);
  if (d) {
    var year = d[3].length === 2 ? '20' + d[3] : d[3];
    out.date = year + '-' + pad2_(d[2]) + '-' + pad2_(d[1]);
  }
  return out;
}

function pad2_(s) { s = String(s); return s.length < 2 ? '0' + s : s; }

// ----------------------------------------------------------------- web app

function doGet() {
  return HtmlService.createHtmlOutput(PAGE_HTML())
    .setTitle('Waverole — כספים')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/** Everything the dashboard needs, in one round trip. */
function api_data() {
  var ss = openBook_(false);
  return apiDataFor_(ss);
}

/**
 * The full dashboard payload. Every mutating api_* function ends by calling
 * this too (merging in its own ok/error/etc) so the client's `boot(r)` after
 * an action always receives a complete DATA object instead of a bare
 * {ok:true} ack that would blank the screen until the next manual refresh.
 */
function apiDataFor_(ss) {
  return {
    ok: true,
    updated: Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'dd/MM/yyyy HH:mm'),
    sheet: ss.getUrl(),
    rate: newestRate_(ss),
    sales: tableOf_(ss, T_SALES),
    expenses: tableOf_(ss, T_EXPENSES),
    monthly: tableOf_(ss, T_MONTHLY),
    packages: tableOf_(ss, T_PACKAGES),
    recon: tableOf_(ss, T_RECON),
    categories: CATEGORIES
  };
}

/** Newest USD->ILS rate on file, for the "מקורות הנתונים" panel. Not the
 *  per-day lookup rateFor_() does — just whatever is freshest. */
function newestRate_(ss) {
  var sh = ss.getSheetByName(T_RATES);
  if (!sh || sh.getLastRow() < 2) return null;
  var values = sh.getRange(2, 1, sh.getLastRow() - 1, 2).getValues();
  var newest = null;
  values.forEach(function (r) {
    var d = String(r[0]), v = parseFloat(r[1]);
    if (!d || isNaN(v)) return;
    if (!newest || d > newest.d) newest = { d: d, v: v };
  });
  return newest ? newest.v : null;
}

function tableOf_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh || sh.getLastRow() < 1) return { header: [], rows: [] };
  var values = sh.getDataRange().getDisplayValues();
  return { header: values[0] || [], rows: values.slice(1) };
}

function api_addExpense(e) {
  var ss = openBook_(false);
  try {
    var sheet = ss.getSheetByName(T_EXPENSES);
    var when = e.date ? new Date(e.date + 'T12:00:00') : new Date();
    var amount = parseFloat(e.amount);
    if (isNaN(amount) || amount <= 0) {
      return fail_(ss, 'סכום לא תקין');
    }
    var row = expenseRow_({
      key: 'man-' + Utilities.getUuid().slice(0, 12),
      when: when,
      vendor: e.vendor || 'לא צוין',
      description: e.description || '',
      category: e.category || 'אחר',
      amount: amount,
      currency: e.currency || 'ILS',
      source: e.receipt ? 'צילום' : 'ידני',
      status: CONFIRMED,
      receipt: e.receipt || '',
      note: e.note || ''
    });
    sheet.appendRow(row);
    return apiDataFor_(ss);
  } catch (err) {
    return fail_(ss, String(err));
  }
}

/** Confirm or drop a row the mail reader was unsure about. */
function api_reviewExpense(key, decision) {
  var ss = openBook_(false);
  try {
    var sheet = ss.getSheetByName(T_EXPENSES);
    var last = sheet.getLastRow();
    if (last < 2) return fail_(ss, 'אין שורות');
    var keys = sheet.getRange(2, 1, last - 1, 1).getValues();
    for (var i = 0; i < keys.length; i++) {
      if (String(keys[i][0]) === String(key)) {
        var rowIndex = i + 2;
        if (decision === 'delete') sheet.deleteRow(rowIndex);
        else sheet.getRange(rowIndex, 10).setValue(CONFIRMED);
        return apiDataFor_(ss);
      }
    }
    return fail_(ss, 'לא נמצא');
  } catch (err) {
    return fail_(ss, String(err));
  }
}

/** apiDataFor_(ss) with ok/error overlaid, so a failed action still hands
 *  the client a complete DATA object instead of blanking the screen. */
function fail_(ss, message) {
  var out = apiDataFor_(ss);
  out.ok = false;
  out.error = message;
  return out;
}

/** An .xlsx of the whole book, saved to Drive, for the accountant. */
function api_export() {
  try {
    var ss = openBook_(false);
    var url = 'https://docs.google.com/spreadsheets/d/' + ss.getId() +
              '/export?format=xlsx';
    var res = UrlFetchApp.fetch(url, {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true
    });
    if (res.getResponseCode() !== 200) {
      return { ok: false, error: 'ייצוא נכשל (' + res.getResponseCode() + ')' };
    }
    var stamp = Utilities.formatDate(new Date(), 'Asia/Jerusalem', 'yyyy-MM-dd');
    var blob = res.getBlob().setName('Waverole-כספים-' + stamp + '.xlsx');
    var file = openFolder_(true).createFile(blob);
    return { ok: true, url: file.getUrl(), name: blob.getName() };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function api_scanNow() {
  var ss = openBook_(false);
  try {
    var added = scanMailbox_(120);
    var out = apiDataFor_(ss);
    out.added = added;
    return out;
  } catch (err) {
    return fail_(ss, String(err));
  }
}

// -------------------------------------------------------------------- page
//
// The interface lives in finance_ui.html so it can be opened and tested in a
// real browser instead of being trapped in a string in this file.

function PAGE_HTML() {
  return HtmlService.createHtmlOutputFromFile('finance_ui').getContent();
}
