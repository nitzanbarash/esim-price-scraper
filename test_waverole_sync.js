// The price range, as the SHEET applies it on a hand edit (waverole_sync.gs).
//
// The same rule lives twice: price_ranges.py moves the price on the chooser's
// 4-hourly run, applyFee_ moves it the moment the owner types. Two copies that
// disagree make the price flip every four hours, so the first half of this
// file asks Python for thousands of answers and demands the same from the
// script. The second half edits a fake sheet the way the owner does.
//
// Run: node test_waverole_sync.js     (needs python3 beside it, as in CI)

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const { execFileSync } = require('child_process');

let failed = 0, passed = 0;
function check(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { passed++; return; }
  failed++;
  console.log('FAIL ' + name + '\n   got  ' + g + '\n   want ' + w);
}

function load() {
  const ctx = { Logger: { log: function () {} }, SpreadsheetApp: { flush: function () {} } };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'waverole_sync.gs'), 'utf8'), ctx);
  ctx.flushPendingRows_ = function () { return 0; };   // no site in a test
  ctx.queueRows_ = function () {};
  return ctx;
}
const gs = load();

// -- 1. the two copies agree -------------------------------------------
const py = JSON.parse(execFileSync('python3', ['-c', `
import json, random
from price_ranges import reprice, parse_range
random.seed(7)
cases = []
for _ in range(5000):
    gb = random.choice([1, 1, 3, 5, 10, 15, 20, 30, 40, 50, 100])
    lo = round(random.uniform(0.5, 40), 2); hi = round(lo + random.uniform(0, 12), 2)
    if gb == 1 and random.random() < 0.7: lo, hi = 0.99, 1.49
    cost = round(random.uniform(0.2, 30), 2)
    cur = None if random.random() < 0.15 else round(random.uniform(0.5, 55), 2)
    cases.append([gb, cost, cur, lo, hi, reprice(gb, cost, cur, lo, hi)])
cells = ['5.99 - 7.99', '5.99-7.99', ' $5.99 \\u2013 $7.99 ', '5,99 - 7,99', '7.99 - 5.99',
         '0 - 3', '5.99', 'abc', '5.99 ~ 7.99', '\\u200f5.99 - 7.99\\u200e', '5.99 \\u2014 7.99', '']
print(json.dumps({'cases': cases, 'cells': [[c, parse_range(c)] for c in cells]}))
`], { cwd: __dirname, encoding: 'utf8' }));
let disagree = 0;
py.cases.forEach(function (c) {
  if (gs.reprice_(c[0], c[1], c[2], c[3], c[4]) !== c[5]) {
    if (disagree++ < 5) check('reprice_ ' + JSON.stringify(c.slice(0, 5)), gs.reprice_(c[0], c[1], c[2], c[3], c[4]), c[5]);
  }
});
check('reprice_ agrees with price_ranges.reprice on ' + py.cases.length + ' cases', disagree, 0);
py.cells.forEach(function (c) { check('parseRange_ ' + JSON.stringify(c[0]), gs.parseRange_(c[0]), c[1]); });

// -- 2. a fake sheet, edited by hand -----------------------------------
// The live layout (2026-10-07): A sku, C GB, D source, G buy, P profit,
// Q stock, R do-not-touch, S net, T fee, U price, V range, W sale, X tick.
const H = {
  sku: '\u05d7\u05d1\u05d9\u05dc\u05d4 (\u05e7\u05d5\u05d3)', gb: 'GB', source: '\u05de\u05e7\u05d5\u05e8',
  buy: '\u05de\u05d7\u05d9\u05e8 \u05e7\u05e0\u05d9\u05d9\u05d4', profit: '\u05e8\u05d5\u05d5\u05d7 (\u05db\u05d3\u05d0\u05d9\u05d5\u05ea)',
  stock: '\u05d1\u05de\u05dc\u05d0\u05d9/\u05e8\u05d5\u05d5\u05d7\u05d9', guard: '<---- \u05dc\u05d0 \u05dc\u05d2\u05e2\u05ea',
  net: '\u05de\u05d7\u05d9\u05e8 \u05e9\u05dc\u05d9', fee: '\u05e1\u05dc\u05d9\u05e7\u05d4', price: '\u05de\u05d7\u05d9\u05e8 \u05e1\u05d5\u05e4\u05d9',
  range: '\u05d8\u05d5\u05d5\u05d7 \u05de\u05d7\u05d9\u05e8\u05d9\u05dd', sale: '\u05de\u05d1\u05e2\u05e6\u05e2\u05d9\u05dd (\u05d0\u05d7\u05d5\u05d6\u05d9\u05dd)',
  tick: '\u05e0\u05d1\u05d7\u05e8'
};
const COL = { sku: 0, gb: 2, source: 3, buy: 6, profit: 15, stock: 16, guard: 17, net: 18,
              fee: 19, price: 20, range: 21, sale: 22, tick: 23 };
const W = 24;
const TICK = '\u2713';
const SOLD_OUT = '\u05dc\u05d0 \u05d1\u05de\u05dc\u05d0\u05d9';   // the scraper's word, not ours
// The bots' own margin words (top-level consts are not reachable from here).
const UNPROFITABLE = '\u05dc\u05d0 \u05e8\u05d5\u05d5\u05d7\u05d9';
const OVER_RANGE = UNPROFITABLE + ' \u2014 \u05de\u05e2\u05dc \u05d8\u05d5\u05d5\u05d7';
const OVER_CEILING = UNPROFITABLE + ' \u2014 \u05de\u05e2\u05dc \u05ea\u05e7\u05e8\u05d4';

function row(o) {
  const r = new Array(W).fill('');
  Object.keys(o).forEach(function (k) { r[COL[k]] = o[k]; });
  return r;
}
function makeSheet(rows) {
  const head = new Array(W).fill('');
  Object.keys(H).forEach(function (k) { head[COL[k]] = H[k]; });
  const cells = [head].concat(rows);
  const sheet = {
    cells: cells,
    getLastRow: function () { return cells.length; },
    getLastColumn: function () { return W; },
    getSheetId: function () { return 0; },
    getName: function () { return 'main'; },
    getRange: function (r, c, nr, nc) {
      nr = nr || 1; nc = nc || 1;
      const rng = {
        getRow: function () { return r; }, getColumn: function () { return c; },
        getLastRow: function () { return r + nr - 1; }, getLastColumn: function () { return c + nc - 1; },
        getNumRows: function () { return nr; }, getNumColumns: function () { return nc; },
        getSheet: function () { return sheet; },
        getValues: function () {
          const out = [];
          for (let i = 0; i < nr; i++) out.push(cells[r - 1 + i].slice(c - 1, c - 1 + nc));
          return out;
        },
        setValues: function (v) {
          for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) {
            if (c - 1 + j === COL.guard) throw new Error('wrote the do-not-touch column');
            cells[r - 1 + i][c - 1 + j] = v[i][j];
          }
        },
        setValue: function (v) { rng.setValues([[v]]); },
        clearContent: function () { rng.setValues([['']]); },
        setBackground: function () {}
      };
      return rng;
    }
  };
  return sheet;
}
// The owner types `value` into (sheetRow, key); onEditPush runs as the trigger would.
function edit(sheet, sheetRow, key, value) {
  sheet.cells[sheetRow - 1][COL[key]] = value;
  const toasts = [];
  const e = { range: sheet.getRange(sheetRow, COL[key] + 1),
              source: { getSheets: function () { return [sheet]; }, toast: function (m) { toasts.push(m); } } };
  gs.onEditPush(e);
  return toasts;
}
const cell = function (sheet, sheetRow, key) { return sheet.cells[sheetRow - 1][COL[key]]; };

// A 10GB twin: dog row unticked, Stellar row ticked and selling at 5.99.
function twin10() {
  return makeSheet([
    row({ sku: '1.1.10', gb: '10gb', source: 'esim.dog', buy: '$4.10', price: '5.99', range: '5.99 - 7.99' }),
    row({ sku: '1.1.10', gb: '10gb', source: 'Stellar', buy: '(\u20ac3.40) $3.90', price: '5.99',
          range: '5.99 - 7.99', tick: TICK })
  ]);
}

// The buy price rises on the ticked row -> the price climbs to the first step paying 30%.
let s = twin10();
let t = edit(s, 3, 'buy', '(\u20ac4.10) $4.80');
check('dearer cost moves the price up', [cell(s, 2, 'price'), cell(s, 3, 'price')], [6.99, 6.99]);
check('...and the net and fee follow, on both rows',
      [cell(s, 2, 'net'), cell(s, 3, 'fee')], [6.36, 0.8]);
check('...and the owner is told why', t.length === 1 && t[0].indexOf('5.99 \u2192 6.99') >= 0, true);
check('...and P is the new margin', cell(s, 3, 'profit'), '\ud83d\udfe2 +$1.56 (+32.5%)');

// The cost falls back -> no drop until the margin passes 200% (hard down).
edit(s, 3, 'buy', '(\u20ac3.40) $3.90');
check('cheaper cost under 200% leaves the price', cell(s, 3, 'price'), 6.99);
edit(s, 3, 'buy', '$1.50');
check('over 200% steps down ONE step', cell(s, 3, 'price'), 6.49);

// The unticked row's cost is not the site's cost: the price does not move.
s = twin10();
edit(s, 2, 'buy', '$5.50');
check('a cost typed on the unticked row moves nothing', cell(s, 3, 'price'), '5.99');
check('...but that row is judged on its own cost', cell(s, 2, 'stock'), '');

// A typed price that pays stays verbatim; one outside the range is pulled in.
s = twin10();
edit(s, 3, 'price', '7.49');
check('a paying price inside the range stays as typed', [cell(s, 2, 'price'), cell(s, 3, 'price')], ['7.49', '7.49']);
edit(s, 3, 'price', '9.99');
check('a price over the range is pulled to the top', cell(s, 3, 'price'), 7.99);
edit(s, 3, 'price', '6.75');
check('an off-grid price snaps up to .99', cell(s, 3, 'price'), 6.99);

// A new range typed on the TWIN row reaches the ticked row and is obeyed.
s = twin10();
edit(s, 2, 'range', '6.49 - 8.49');
check('a range typed on the twin row is copied to every row',
      [cell(s, 2, 'range'), cell(s, 3, 'range')], ['6.49 - 8.49', '6.49 - 8.49']);
check('...and the price moves into it', cell(s, 3, 'price'), 6.49);

// Nothing in the range pays -> the price stays, Q says over-range; and back.
s = twin10();
edit(s, 3, 'buy', '$6.50');
check('nothing pays: the price stays', cell(s, 3, 'price'), '5.99');
check('...and Q says over the range', cell(s, 3, 'stock'), OVER_RANGE);
edit(s, 3, 'buy', '$4.00');
check('a cost that pays again clears it', cell(s, 3, 'stock'), '');

// A word somebody else wrote in Q is never touched.
s = twin10();
s.cells[2][COL.stock] = SOLD_OUT;
edit(s, 3, 'buy', '$6.50');
check("the scraper's sold-out word stays", cell(s, 3, 'stock'), SOLD_OUT);

// Blank range = hands off: the typed price stands, the old 20% floor judges.
s = twin10();
edit(s, 3, 'range', '');
check('clearing the range clears it on every row', [cell(s, 2, 'range'), cell(s, 3, 'range')], ['', '']);
edit(s, 3, 'price', '4.99');
check('with no range a typed price is kept', cell(s, 3, 'price'), '4.99');
check('...and the old floor calls it unprofitable', cell(s, 3, 'stock'), UNPROFITABLE);

// A sale-only edit copies the sale and nothing else.
s = twin10();
const before = JSON.stringify(s.cells);
edit(s, 3, 'sale', 10);
check('a sale edit reaches the twin', cell(s, 2, 'sale'), 10);
s.cells[1][COL.sale] = ''; s.cells[2][COL.sale] = '';
check('...and changes nothing else', JSON.stringify(s.cells), before);

// Moving the tick prices the package at the NEW row's cost.
s = twin10();
edit(s, 2, 'buy', '$4.50');           // 5.99 pays 38% at Stellar's cost, 20% at this one
check('a cost on the unticked row still moves nothing', cell(s, 3, 'price'), '5.99');
edit(s, 3, 'tick', '');
edit(s, 2, 'tick', TICK);
check('the tick moves to the dog row', [cell(s, 2, 'tick'), cell(s, 3, 'tick')], [TICK, '']);
check("...and the price follows the dog row's cost", cell(s, 2, 'price'), 6.49);

// 1GB: the lowest step losing at most 10 cents, wherever it is now.
s = makeSheet([row({ sku: '1.1.1', gb: '1gb', source: 'Stellar', buy: '$0.87', price: '1.49',
                     range: '0.99 - 1.49', tick: TICK })]);
edit(s, 2, 'buy', '$0.71');
check('1GB drops to the lowest paying step', cell(s, 2, 'price'), 1.09);
edit(s, 2, 'buy', '$1.19');
check('1GB with nothing paying stays and goes off sale',
      [cell(s, 2, 'price'), cell(s, 2, 'stock')], [1.09, OVER_RANGE]);

// The buy ceiling is judged first, as in the scraper and the chooser.
s = makeSheet([row({ sku: '1.1.30', gb: '30gb', source: 'esim.dog', buy: '$9.00', price: '13.49',
                     range: '13.49 - 17.49', tick: TICK })]);
edit(s, 2, 'buy', '$10.50');
check('a 30GB bought over $10 is over the ceiling', cell(s, 2, 'stock'), OVER_CEILING);

console.log((failed ? 'FAILED ' + failed + ', ' : '') + 'passed ' + passed);
process.exit(failed ? 1 : 0);
