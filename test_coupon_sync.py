#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the row parser of coupon_sync.py.

The sheet is where the owner sets discounts, and every one of these is a way of
not giving away money the row never meant to give away:

    * a blank 'פעיל' cell is NOT active - a half-typed row stays off,
    * a percent cell formatted as a percent reads back 0.1 and must still mean
      10% (the same class of bug as the currency format that once cancelled
      three paid orders - memory: variant-cell-rendering),
    * a date is day-first when it is ambiguous, and an expiry runs to the END
      of the day the owner wrote,
    * an unreadable cell drops its own row and leaves the rest of the sheet
      syncing.

Run:  python test_coupon_sync.py
"""

import datetime as dt
import sys

from coupon_sync import (
    HEADERS, SOURCE_HE, TZ, norm_code, parse_bool, parse_date, parse_int,
    parse_list, parse_num, parse_pct, parse_rows, plan_writes, public,
    row_to_input,
)

_fails: list[str] = []
_n = 0


def check(name, got, want):
    global _n
    _n += 1
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


def raises(name, fn):
    global _n
    _n += 1
    try:
        fn()
    except ValueError:
        print(f"  ok   {name}")
        return
    print(f"  FAIL {name}  (no ValueError)")
    _fails.append(name)


def row(**kw):
    """A sheet row keyed by header text; anything unnamed is blank."""
    r = {h: "" for h in HEADERS}
    named = dict(zip(
        ["code", "pct", "fixed", "cap", "min_gb", "max_gb", "exclude", "only",
         "max_uses", "per_customer", "starts", "expires", "active", "email",
         "label", "note"], HEADERS[:16]))
    for k, v in kw.items():
        r[named[k]] = v
    return r


print("\n-- the code itself --")
check("trimmed and upcased", norm_code("  wave10 "), "WAVE10")
check("a hyphen is not a code", norm_code("WAVE-10"), "")
check("one character is not a code", norm_code("A"), "")
check("24 characters is still a code", norm_code("A" * 24), "A" * 24)
check("25 is not", norm_code("A" * 25), "")
check("a number typed as a code survives", norm_code(2026), "2026")

print("\n-- yes / no, in every form the owner types --")
for yes in ["כן", "yes", "TRUE", "true", 1, True, "V", "✓"]:
    check(f"active: {yes!r}", parse_bool(yes), True)
for no in ["לא", "no", "FALSE", 0, False, "", "   "]:
    check(f"inactive: {no!r}", parse_bool(no), False)
check("a word nobody meant is not active", parse_bool("maybe"), False)

print("\n-- numbers --")
check("a plain number", parse_num(12.5, "x"), 12.5)
check("a blank is nothing, not zero", parse_num("", "x"), None)
check("a typed dollar sign is stripped", parse_num("$5", "x"), 5.0)
check("a typed percent sign is stripped", parse_pct("10%", "x"), 10.0)
check("a percent FORMAT reads 0.1 and means 10", parse_pct(0.1, "x"), 10.0)
check("a percent format at 20", parse_pct(0.2, "x"), 20.0)
check("1 stays 1 percent", parse_pct(1, "x"), 1.0)
check("100 stays 100", parse_pct(100, "x"), 100.0)
raises("101 percent is refused", lambda: parse_pct(101, "x"))
raises("a negative percent is refused", lambda: parse_pct(-5, "x"))
raises("words are not a percent", lambda: parse_pct("ten", "x"))
check("a whole number", parse_int("3", "x"), 3)
check("a whole number typed as a float", parse_int(3.0, "x"), 3)
raises("2.5 uses is refused", lambda: parse_int(2.5, "x"))

print("\n-- the sku lists --")
check("commas", parse_list("1.81.10, 1.81.20"), ["1.81.10", "1.81.20"])
check("newlines and semicolons too", parse_list("81;\n1.39"), ["81", "1.39"])
check("a blank list is an empty array", parse_list(""), [])
check("stray commas do not become empty entries", parse_list("81,,"), ["81"])
check("a bare country code arrives as a number", parse_list(81), ["81"])

print("\n-- dates --")
check("day-first when ambiguous", parse_date("03/09/2026")[:10], "2026-09-03")
check("iso stays iso", parse_date("2026-09-03")[:10], "2026-09-03")
check("dots work too", parse_date("3.9.2026")[:10], "2026-09-03")
check("a two-digit year is this century", parse_date("3/9/26")[:10], "2026-09-03")
check("a date object", parse_date(dt.date(2026, 9, 3))[:10], "2026-09-03")
check("a sheets serial", parse_date(46268)[:10], "2026-09-03")
check("a blank is nothing", parse_date(""), None)
check("a start opens at midnight local",
      parse_date("03/09/2026"),
      dt.datetime(2026, 9, 3, 0, 0, 0, tzinfo=TZ).isoformat())
check("an expiry runs to the end of that day",
      parse_date("03/09/2026", end_of_day=True),
      dt.datetime(2026, 9, 3, 23, 59, 59, tzinfo=TZ).isoformat())
check("an expiry carries an offset, so it is not UTC midnight",
      parse_date("03/09/2026", end_of_day=True)[-6:] in ("+03:00", "+02:00"), True)
check("an instant is passed through untouched",
      parse_date("2026-09-03T12:00:00Z"), "2026-09-03T12:00:00Z")
raises("31/02 is not a real date", lambda: parse_date("31/02/2026"))
raises("a word is not a date", lambda: parse_date("soon"))

print("\n-- a whole row --")
inp = row_to_input(row(code=" wave10 ", pct=10, cap=8, min_gb=2,
                       exclude="1.81, 39", max_uses=100, per_customer=1,
                       expires="30/09/2026", active="כן",
                       label="  10% off  ", note="autumn"))
check("code", inp["code"], "WAVE10")
check("pct", inp["pct"], 10.0)
check("fixed is null when the cell is blank", inp["fixed_usd"], None)
check("cap", inp["max_off_usd"], 8.0)
check("min gb is inclusive and stays a number", inp["min_gb"], 2.0)
check("exclude is an array", inp["exclude"], ["1.81", "39"])
check("only is an empty array, not null", inp["only"], [])
check("max uses", inp["max_uses"], 100)
check("per customer", inp["per_customer"], 1)
check("active", inp["active"], True)
check("no email means no binding", inp["bound_email"], None)
check("label is trimmed", inp["label"], "10% off")
check("note", inp["note"], "autumn")
check("starts is null", inp["starts_at"], None)
check("every field the server names is present",
      sorted(inp), sorted(["code", "pct", "fixed_usd", "max_off_usd", "min_gb",
                           "max_gb", "exclude", "only", "max_uses",
                           "per_customer", "starts_at", "expires_at", "active",
                           "bound_email", "label", "note"]))

check("an email is lower-cased for the binding",
      row_to_input(row(code="WR1", email="  Someone@Example.COM "))["bound_email"],
      "someone@example.com")
check("a label longer than 40 is cut",
      len(row_to_input(row(code="WR1", label="x" * 60))["label"]), 40)
check("a row with no code is not a coupon", row_to_input(row(pct=10)), None)
raises("a code the till would refuse stops the row",
       lambda: row_to_input(row(code="WAVE-10")))

print("\n-- the sheet as a whole --")
vals = [
    HEADERS,
    ["WAVE10", 10, "", "", "", "", "", "", "", "", "", "", "כן", "", "", "", "", "", ""],
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["TRAVEL5", "", 5, "", "", "", "", "", "", "", "", "", "לא", "", "", "", "", "", ""],
    ["BADPCT", "ten", "", "", "", "", "", "", "", "", "", "", "כן", "", "", "", "", "", ""],
    ["WAVE10", 20, "", "", "", "", "", "", "", "", "", "", "כן", "", "", "", "", "", ""],
    ["WRQ7K2M9AB", 15, "", "", "", "", "", "", 1, 1, "", "10/03/2027", "כן", "someone@example.com", "", "", 0, SOURCE_HE["auto"], ""],
    ["WRZ2Z2Z2Z2", 15, "", "", "", "", "", "", 1, 1, "", "10/03/2027", "כן", "other@example.com", "", "", "", "", ""],
    ["EXPIREDONE", 10, "", "", "", "", "", "", "", "", "", "", "כן", "", "", "", "", SOURCE_HE["sheet"], "old"],
]
inputs, problems, idx = parse_rows(vals)
check("blank rows are skipped, broken rows are dropped, the server's own rows are never pushed",
      [i["code"] for i in inputs], ["WAVE10", "TRAVEL5", "EXPIREDONE"])
check("a server row is not a problem row either", any("row 7" in p or "row 8" in p for p in problems), False)
check("the broken row is reported", any("BADPCT" in p or "row 5" in p for p in problems), True)
check("a duplicate code is refused, not silently last-wins",
      any("row 6" in p for p in problems), True)
check("an inactive row still syncs (that is how it is switched off)",
      [i["active"] for i in inputs], [True, False, True])
check("the header index is by name", idx[HEADERS[0]], 0)
raises("an empty tab is not a sheet", lambda: parse_rows([]))
raises("a header missing a column stops the run",
       lambda: parse_rows([HEADERS[:5], ["WAVE10"]]))

print("\n-- what goes back into the grey columns --")
server = [
    {"code": "WAVE10", "source": "sheet", "uses": 7},
    {"code": "WR2K7M9QAB", "source": "auto", "uses": 0, "pct": 15,
     "bound_email": "someone@example.com", "expires_at": "2027-03-10T00:00:00Z",
     "max_uses": 1, "per_customer": 1, "active": True, "exclude": [], "only": []},
]
entries, added = plan_writes(vals, idx, server, "2026-09-11 03:00")
check("Q,R,S go out as one contiguous range", entries[0]["range"], "Q2:S9")
check("a synced row carries uses, source and a stamp",
      entries[0]["values"][0], [7, SOURCE_HE["sheet"], "2026-09-11 03:00"])
check("a row the site does not know is left blank",
      entries[0]["values"][1], ["", "", ""])
check("a code the site no longer holds loses its count and stamp but keeps its source",
      entries[0]["values"][7], ["", SOURCE_HE["sheet"], ""])
check("an expired personal row keeps saying it is the server's",
      entries[0]["values"][5], ["", SOURCE_HE["auto"], ""])
check("the appended row lands after the last row", entries[1]["range"], "A10:S10")
check("a personal code is written to the sheet with its binding",
      [entries[1]["values"][0][0], entries[1]["values"][0][13],
       entries[1]["values"][0][17]],
      ["WR2K7M9QAB", "someone@example.com", SOURCE_HE["auto"]])
check("its expiry is written as a plain date",
      entries[1]["values"][0][11], "2027-03-10")
check("but its NAME never reaches the log", added, ["(אישי)"])
check("a sheet code is safe to name", public("WAVE10", "sheet"), "WAVE10")
check("a builtin is safe to name", public("CAPYBARA", "builtin"), "CAPYBARA")
check("a personal code is not", public("WR2K7M9QAB", "auto"), "(אישי)")
# The append writes personal codes INTO the sheet, so the next run reads them
# back as ordinary rows with no source at all. Two signs are left.
check("a minted code is recognised by its shape alone",
      public("WR2K7M9QAB"), "(אישי)")
check("so is anything bound to an email",
      public("THANKYOU", bound_email="buyer@example.com"), "(אישי)")
check("a code that only looks similar is still public", public("WRAP"), "WRAP")

entries2, added2 = plan_writes([HEADERS], idx, [], "2026-09-11 03:00")
check("an empty tab writes nothing at all", entries2, [])
check("and appends nothing", added2, [])

if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print(f"\n{_n} checks passed")
sys.exit(0)
