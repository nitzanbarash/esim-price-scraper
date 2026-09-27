"""The receipts sheet's colours: who sold it, and what the customer paid.

Two columns carry a colour the owner reads at a glance (set 2026-09-27):

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

They are conditional-format rules, not painted cells, so a row is coloured the
moment ANY writer adds it — the PC bot, the Stellar buyer, a row typed by hand
— and a corrected price re-colours itself. The colours are the ones the owner
painted by hand before this existed.

Rules are found by the column's HEADER, because the owner reorders columns.
This script owns every rule whose range is exactly one of those two columns
from row 2 to the grid's last row, and nothing else (the Status column keeps its own rules).
It is idempotent: when the sheet already matches, it writes nothing.

    python receipts_colors.py            # apply
    python receipts_colors.py --check    # print what would change, write nothing

usage.yml runs it every 4 hours, which is how a new supplier gets its colour.
"""

import json
import sys

from fulfillment_bot import RECEIPTS_SHEET_ID, sheet_client

COL_SOURCE = "מקור - source"
COL_SELL = "מכירה - Sell"
COL_BUY = "קנייה - Buy"
COL_SALE = "הנחה - Sale"

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


def _num(ref: str) -> str:
    """The first number in a cell, whether it holds 6, '6$' or '14.51$'."""
    return ('VALUE(REGEXEXTRACT(SUBSTITUTE(TO_TEXT(%s),",",""),'
            '"[0-9]+(?:\\.[0-9]+)?"))' % ref)


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
    missing = [c for c in (COL_SOURCE, COL_SELL, COL_BUY, COL_SALE) if c not in col]
    if missing:
        raise SystemExit(f"receipts sheet has no column {missing} — nothing changed")
    src, sell, buy, sale = (col[c] for c in (COL_SOURCE, COL_SELL, COL_BUY, COL_SALE))
    V, Z, X, Y = ("$%s2" % _letter(i) for i in (src, sell, buy, sale))

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
