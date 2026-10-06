"""The receipts sheet's colours: who sold it, what the customer paid, and
how much of the package is gone.

Three columns carry a colour the owner reads at a glance (the first two set
2026-09-27, the usage bands 2026-10-06):

  'מקור - source'  the WORD is coloured (no fill), one colour per SUPPLIER —
                   Stellar blue, esim.dog purple, as the owner typed them.
                   A supplier added to the column's dropdown later gets the
                   next colour from PALETTE on the next run, with no code
                   change; give it a fixed colour in SUPPLIER_COLORS if it
                   should keep one.
  'מכירה - Sell'   the cell is FILLED:
                   red   = given away (0) or sold below its buy price,
                   darker red = 'refund',
                   blue-grey = sold with a discount ('הנחה - Sale' says N%),
                   green = paid in full.
  'GB (0/X) - ניצול'  the cell is FILLED by how much of "used / total" is gone:
                   grey = untouched (0), green = up to half, yellow = past
                   half, red = past 80%, blue = finished (used >= total).
                   These are the bands the owner painted by hand until now;
                   usage_band() below is the same rule in Python.

They are conditional-format rules, not painted cells, so a row is coloured the
moment ANY writer adds it — the PC bot, the Stellar buyer, a row typed by hand
— and a corrected price re-colours itself. The colours are the ones the owner
painted by hand before this existed.

Rules are found by the column's HEADER, because the owner reorders columns.
This script owns every rule whose range is exactly one of those three columns
from row 2 to the grid's last row, and nothing else (the Status column keeps its own rules).
It is idempotent: when the sheet already matches, it writes nothing.

    python receipts_colors.py            # apply
    python receipts_colors.py --check    # print what would change, write nothing

usage.yml runs it every 4 hours, which is how a new supplier gets its colour.
"""

import json
import re
import sys

from fulfillment_bot import RECEIPTS_SHEET_ID, sheet_client

COL_SOURCE = "מקור - source"
COL_SELL = "מכירה - Sell"
COL_BUY = "קנייה - Buy"
COL_SALE = "הנחה - Sale"
COL_USAGE = "GB (0/X) - ניצול"

# Text colours. Spelled as the dropdown spells them; matched case-insensitively.
SUPPLIER_COLORS = {
    "esim.dog": "#9900ff",   # purple
    "Stellar": "#4a86e8",    # blue
}
# For suppliers without a fixed colour, in the dropdown's order. Kept clear of
# blue/purple (taken) and of red/green (they mean money in the next column).
PALETTE = ["#e69138", "#ff00ff", "#45818e", "#bf9000", "#999999"]

RED_REFUND = "#ea9999"
RED = "#f4cccc"
DISCOUNT = "#d0e0e3"
GREEN = "#d9ead3"

# The usage bands, as the owner painted them. The cell reads "used / total"
# (usage_bot writes f"{used:g} / {total:g}", stellar_buyer "0 / N" at purchase).
USAGE_GREY = "#efefef"      # untouched: used == 0
USAGE_GREEN = GREEN         # used, up to half
USAGE_YELLOW = "#fff2cc"    # past half, up to 80%
USAGE_RED = RED             # past 80%, not finished
USAGE_BLUE = "#c9daf8"      # finished: used >= total
USAGE_HALF, USAGE_HOT = 0.5, 0.8
# One regex each for the two numbers. Both insist the WHOLE cell is
# "number / number" (spaces around the slash or not): anything else -- a bare
# number, words, a second slash -- matches neither and gets no colour. The
# same two strings drive the Sheets formulas and the Python twin, so the two
# cannot drift apart.
_NUM = "[0-9]+(?:\\.[0-9]+)?"
USED_RE = f"^\\s*({_NUM})\\s*/\\s*{_NUM}\\s*$"
TOTAL_RE = f"^\\s*{_NUM}\\s*/\\s*({_NUM})\\s*$"


def _num(ref: str) -> str:
    """The first number in a cell, whether it holds 6, '6$' or '14.51$'."""
    return ('VALUE(REGEXEXTRACT(SUBSTITUTE(TO_TEXT(%s),",",""),'
            '"[0-9]+(?:\\.[0-9]+)?"))' % ref)


def _used(ref: str) -> str:
    """The number before the slash of a 'used / total' cell (an error otherwise)."""
    return 'VALUE(REGEXEXTRACT(TO_TEXT(%s),"%s"))' % (ref, USED_RE)


def _total(ref: str) -> str:
    """The number after the slash of a 'used / total' cell (an error otherwise)."""
    return 'VALUE(REGEXEXTRACT(TO_TEXT(%s),"%s"))' % (ref, TOTAL_RE)


def usage_formulas(ref: str) -> list[tuple[str, str]]:
    """(formula, colour) for the usage column, in the order they are installed.

    Sheets stops at the FIRST rule whose formula is true, but these five do
    not lean on that: each formula is true for exactly one band and false for
    the other four, so the colours survive the owner dragging the rules
    about. The order is still the one a reader expects, from untouched to
    finished-and-beyond:
      grey    used = 0           (a 'total' of 0 lands here, never in blue)
      blue    total > 0, used >= total
      red     0.8*total < used < total
      yellow  0.5*total < used <= 0.8*total
      green   0 < used <= 0.5*total
    Both numbers must parse, or the whole thing is FALSE: a blank cell, a bare
    number, or words raise inside REGEXEXTRACT and IFERROR swallows that.
    Percentages are compared as used > total*k, never used/total > k, so a
    zero total divides nothing and an exact 50% or 80% ('5 / 10', '8 / 10')
    is the lower band, as the owner reads it.
    """
    u, t = _used(ref), _total(ref)
    return [
        (f"=IFERROR(AND({u}=0,{t}>=0),FALSE)", USAGE_GREY),
        (f"=IFERROR(AND({t}>0,{u}>={t}),FALSE)", USAGE_BLUE),
        (f"=IFERROR(AND({u}>{t}*{USAGE_HOT},{u}<{t}),FALSE)", USAGE_RED),
        (f"=IFERROR(AND({u}>{t}*{USAGE_HALF},{u}<={t}*{USAGE_HOT}),FALSE)", USAGE_YELLOW),
        (f"=IFERROR(AND({u}>0,{u}<={t}*{USAGE_HALF}),FALSE)", USAGE_GREEN),
    ]


def usage_band(text) -> str | None:
    """The colour the Sheets rules give a usage cell, computed here: the same
    two regexes, the same comparisons in the same order. None = no band (a
    blank, a bare number, words, or 'used' with a total of 0)."""
    cell = "" if text is None else str(text)
    mu, mt = re.search(USED_RE, cell), re.search(TOTAL_RE, cell)
    if not mu or not mt:
        return None
    u, t = float(mu.group(1)), float(mt.group(1))
    if u == 0 and t >= 0:
        return USAGE_GREY
    if t > 0 and u >= t:
        return USAGE_BLUE
    if u > t * USAGE_HOT and u < t:
        return USAGE_RED
    if u > t * USAGE_HALF and u <= t * USAGE_HOT:
        return USAGE_YELLOW
    if u > 0 and u <= t * USAGE_HALF:
        return USAGE_GREEN
    return None


def _rgb(hexcolor: str) -> dict:
    h = hexcolor.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _hex(rgb: dict) -> str:
    return "#%02x%02x%02x" % tuple(round(rgb.get(k, 0) * 255) for k in ("red", "green", "blue"))


def _letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def supplier_colors(dropdown: list[str]) -> list[tuple[str, str]]:
    """(supplier, colour) for every supplier the column may hold."""
    fixed = {k.lower(): v for k, v in SUPPLIER_COLORS.items()}
    out, seen, k = [], set(), 0
    for name in list(SUPPLIER_COLORS) + list(dropdown):
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if key in fixed:
            color = fixed[key]
        else:
            color = PALETTE[k % len(PALETTE)]
            k += 1
        out.append((name.strip(), color))
    return out


def desired_rules(header: list[str], dropdown: list[str], sheet_id: int,
                  rows: int) -> list[dict]:
    """The rules, in priority order (the first one that matches a cell wins)."""
    col = {h: i for i, h in enumerate(header)}
    missing = [c for c in (COL_SOURCE, COL_SELL, COL_BUY, COL_SALE, COL_USAGE) if c not in col]
    if missing:
        raise SystemExit(f"receipts sheet has no column {missing} — nothing changed")
    src, sell, buy, sale, use = (col[c] for c in (COL_SOURCE, COL_SELL, COL_BUY, COL_SALE, COL_USAGE))
    V, Z, X, Y, E = ("$%s2" % _letter(i) for i in (src, sell, buy, sale, use))

    def rule(column, formula, color, text=False):
        fmt = ({"textFormat": {"foregroundColor": _rgb(color)}} if text
               else {"backgroundColor": _rgb(color)})
        return {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": rows,
                        "startColumnIndex": column, "endColumnIndex": column + 1}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": fmt,
            },
        }

    rules = []
    for name, color in supplier_colors(dropdown):
        lit = name.lower().replace('"', '""')
        rules.append(rule(src, f'=LOWER(TRIM({V}))="{lit}"', color, text=True))
    rules += [
        rule(sell, f'=REGEXMATCH(LOWER(TO_TEXT({Z})),"refund")', RED_REFUND),
        rule(sell, f"=IFERROR({_num(Z)}=0,FALSE)", RED),
        rule(sell, f"=IFERROR({_num(Z)}<{_num(X)},FALSE)", RED),
        rule(sell, f'=AND(LEN(TO_TEXT({Z}))>0,IFERROR(VALUE(REGEXEXTRACT('
                   f'TO_TEXT({Y}),"^\\s*([0-9]+(?:\\.[0-9]+)?)\\s*%"))>0,FALSE))', DISCOUNT),
        rule(sell, f"=IFERROR({_num(Z)}>0,FALSE)", GREEN),
    ]
    rules += [rule(use, formula, color) for formula, color in usage_formulas(E)]
    return rules


def _key(r: dict) -> str:
    """A rule reduced to what matters, so the sheet's copy compares equal."""
    rng = r["ranges"][0]
    b = r["booleanRule"]
    return json.dumps([
        rng.get("startRowIndex", 0), rng.get("endRowIndex"),
        rng["startColumnIndex"], rng["endColumnIndex"],
        b["condition"]["type"],
        [v.get("userEnteredValue") for v in b["condition"].get("values", [])],
        _hex(b["format"]["backgroundColor"]) if "backgroundColor" in b["format"] else None,
        _hex(b["format"].get("textFormat", {}).get("foregroundColor", {}))
        if "textFormat" in b["format"] else None,
    ], ensure_ascii=False)


def _owned(r: dict, columns: set[int]) -> bool:
    rngs = r.get("ranges", [])
    return (len(rngs) == 1 and "booleanRule" in r
            and rngs[0].get("startRowIndex", 0) == 1
            and rngs[0].get("startColumnIndex") in columns
            and rngs[0].get("endColumnIndex") == rngs[0].get("startColumnIndex") + 1)


def plan(meta: dict, header: list[str], dropdown: list[str]) -> list[dict]:
    """The batchUpdate requests that make the sheet match; [] when it already does."""
    sheet_id = meta["properties"]["sheetId"]
    # "To the bottom" is stored as the grid's last row, and a rule does not
    # follow the grid when an append grows it: re-cover the whole grid.
    rows = meta["properties"]["gridProperties"]["rowCount"]
    want = desired_rules(header, dropdown, sheet_id, rows)
    cols = {r["ranges"][0]["startColumnIndex"] for r in want}
    have = meta.get("conditionalFormats", [])
    mine = [i for i, r in enumerate(have) if _owned(r, cols)]
    if [_key(have[i]) for i in mine] == [_key(r) for r in want]:
        return []
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
            for i in reversed(mine)]
    base = len(have) - len(mine)
    reqs += [{"addConditionalFormatRule": {"rule": r, "index": base + n}}
             for n, r in enumerate(want)]
    return reqs


def main() -> int:
    check = "--check" in sys.argv[1:]
    ss = sheet_client().open_by_key(RECEIPTS_SHEET_ID)
    ws = ss.sheet1
    meta = next(s for s in ss.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId,gridProperties),conditionalFormats)"})["sheets"]
        if s["properties"]["sheetId"] == ws.id)
    header = ws.row_values(1)
    src = header.index(COL_SOURCE) if COL_SOURCE in header else None
    dropdown = []
    if src is not None:
        cell = f"{ws.title}!{_letter(src)}2"
        got = ss.fetch_sheet_metadata(params={
            "ranges": cell, "includeGridData": "true",
            "fields": "sheets.data.rowData.values.dataValidation"})
        try:
            dv = got["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"]
            dropdown = [v.get("userEnteredValue", "") for v in dv["condition"].get("values", [])]
        except (KeyError, IndexError):
            dropdown = []
    reqs = plan(meta, header, dropdown)
    colors = ", ".join(f"{n} {c}" for n, c in supplier_colors(dropdown))
    if not reqs:
        print(f"receipts colours already in place ({colors})")
        return 0
    if check:
        print(f"would rewrite the colour rules ({colors}):")
        print(json.dumps(reqs, ensure_ascii=False, indent=1))
        return 0
    ss.batch_update({"requests": reqs})
    print(f"receipts colour rules written ({colors})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
