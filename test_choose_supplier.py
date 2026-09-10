#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for which supplier's row a SKU is sold on.

decide() is pure — rows in, decisions out — so every rule the owner gave can be
pinned here without a sheet, a network or a credential:

    * a rival that is 0.9% cheaper does NOT move a live SKU; 1.1% does
      (day_policy.DAY_TOL, the same 1% the validity rules use),
    * an incumbent that cannot be sold — a word in במלאי/רווחי, or no price —
      yields to any eligible challenger, however small the gap,
    * an ambiguous SKU (two ticks, two esim.dog rows, neither) is SKIPPED and
      not one of its cells is written,
    * a one-supplier SKU is not a choice and is never touched,
    * a BLANK מקור is esim.dog, so the oldest rows in the sheet defend
      themselves instead of losing to any Stellar price at all,
    * a switch carries the customer side (U,S,T,V) across — and if the TICKED
      incumbent's U is not a readable price, the SKU is skipped rather than
      moved onto a row with no customer price,
    * a carried cell is written with the Python TYPE it was read as,
    * רווח is filled in on the blank rows of every two-supplier SKU, in the
      scraper's format, off the SKU's מחיר שלי.

Run:  python test_choose_supplier.py
"""

import sys

import day_policy
from choose_supplier import (
    CARRY, LAST_COL, LOSER_BG, TICK, WINNER_BG, Row, _entered_value,
    cap_switches, decide, eligible, final_usd, money, profit_text, source_of,
    switch_note, usd,
)

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


def dog(row, sku, price, **kw):
    kw.setdefault("validity", "30d")
    kw.setdefault("chosen", TICK)
    kw.setdefault("my_price", "5.40")
    kw.setdefault("fee", "0.8")
    kw.setdefault("final", "5.99")
    kw.setdefault("discount", "10%")
    kw.setdefault("profit", "🟢 +$2.32 (+75.3%)")
    return Row(row=row, sku=sku, source="esim.dog", price=price, **kw)


def stellar(row, sku, price, **kw):
    kw.setdefault("validity", "30d")
    kw.setdefault("chosen", "")
    return Row(row=row, sku=sku, source="Stellar", price=price, **kw)


def only(decisions):
    check("exactly one decision", len(decisions), 1)
    return decisions[0]


def writes_of(d, key):
    return [(row, text) for row, k, text in d.writes if k == key]


print("-- the price cell: dollars first, and a bare number is not a price --")
check("plain", usd("$0.58"), 0.58)
check("two currencies", usd("$0.56 (€0.48)"), 0.56)
check("em dash", usd("—"), None)
check("blank", usd(""), None)
check("bare number is refused", usd("0.56"), None)
check("euro-first is refused", usd("€0.48 ($0.56)"), None)

print("\n-- eligibility --")
check("a stocked, priced, dated row is eligible", eligible(dog(2, "1.1.1", "$1.00")), True)
check("a word in Q makes it ineligible",
      eligible(dog(2, "1.1.1", "$1.00", stock="לא רווחי")), False)
check("no price makes it ineligible", eligible(dog(2, "1.1.1", "—")), False)
check("no days makes it ineligible", eligible(dog(2, "1.1.1", "$1.00", validity="")), False)
check("an unknown supplier is never eligible",
      eligible(Row(row=2, sku="1.1.1", source="citrus", price="$1.00", validity="30d")), False)

print("\n-- the 1% gate (day_policy.DAY_TOL) --")
check("tolerance is the day policy's own", day_policy.DAY_TOL, 0.01)
d = only(decide([dog(2, "1.66.10", "$10.00"), stellar(3, "1.66.10", "$9.91")]))
check("0.9% cheaper does not move the SKU", d.action, "keep")
check("...and writes no tick", writes_of(d, "chosen"), [])
check("...and writes no note", writes_of(d, "changed"), [])

d = only(decide([dog(2, "1.66.10", "$10.00"), stellar(3, "1.66.10", "$9.89")]))
check("1.1% cheaper does move it", d.action, "switch")
check("the tick lands on the Stellar row", writes_of(d, "chosen"), [(3, TICK)] + [(2, "")])
check("the note says both suppliers and both costs",
      writes_of(d, "changed"), [(3, "↔ ספק: esim.dog → Stellar ($10.00 → $9.89)")])
check("green on the winner, grey on the loser",
      d.colours, [(3, WINNER_BG), (2, LOSER_BG)])

d = only(decide([dog(2, "1.66.10", "$10.00"), stellar(3, "1.66.10", "$10.00")]))
check("an equal price stays put", d.action, "keep")

print("\n-- an incumbent that cannot be sold yields for any gap --")
d = only(decide([dog(2, "1.66.10", "$10.00", stock="לא רווחי"),
                 stellar(3, "1.66.10", "$9.99")]))
check("out of stock yields to a challenger 0.1% cheaper", d.action, "switch")
check("...to the Stellar row", d.winner.row, 3)
d = only(decide([dog(2, "1.66.10", "—"), stellar(3, "1.66.10", "$99.00")]))
check("no price yields even to a DEARER challenger", d.action, "switch")
d = only(decide([dog(2, "1.66.10", "$10.00", stock="לא רווחי"),
                 stellar(3, "1.66.10", "$9.99", stock="לא זמין")]))
check("both unsellable: nothing moves", d.action, "keep")
check("...and nothing is ticked", writes_of(d, "chosen"), [])

print("\n-- the cheapest challenger, and ties go to the longer plan --")
d = only(decide([dog(2, "1.0B.10", "$10.00"),
                 stellar(3, "1.0B.10", "$8.00", validity="20d"),
                 stellar(4, "1.0B.10", "$8.00", validity="30d")]))
check("a tie at $8.00 takes the 30-day row", d.winner.row, 4)
d = only(decide([dog(2, "1.0B.10", "$10.00"),
                 stellar(3, "1.0B.10", "$7.50", validity="20d"),
                 stellar(4, "1.0B.10", "$8.00", validity="30d")]))
check("cheaper beats longer", d.winner.row, 3)

print("\n-- an ambiguous SKU is skipped, never guessed --")
d = only(decide([dog(2, "1.66.10", "$10.00"), stellar(3, "1.66.10", "$5.00", chosen=TICK)]))
check("two ticks -> skipped", d.action, "skip")
check("...with a reason", "ticked" in d.reason, True)
check("...and not one cell written", d.writes, [])
check("...and no colour either", d.colours, [])

d = only(decide([dog(2, "1.66.10", "$10.00", chosen=""),
                 dog(3, "1.66.10", "$5.00", chosen="")]))
check("no tick and two esim.dog rows -> skipped", d.action, "skip")
check("...and nothing written", d.writes, [])

d = only(decide([stellar(2, "1.66.10", "$10.00"), stellar(3, "1.66.10", "$5.00")]))
check("no tick and no esim.dog row -> skipped", d.action, "skip")
check("...and nothing written", d.writes, [])

print("\n-- with no tick, esim.dog is the incumbent --")
d = only(decide([dog(2, "1.66.10", "$10.00", chosen=""), stellar(3, "1.66.10", "$9.95")]))
check("the unticked dog row is the incumbent", d.incumbent.row, 2)
check("...and 0.5% does not unseat it", d.action, "keep")
d = only(decide([dog(2, "1.66.10", "$10.00", chosen=""), stellar(3, "1.66.10", "$8.00")]))
check("a real gap does", d.action, "switch")
check("the tick is written even though none existed", writes_of(d, "chosen"), [(3, TICK)])

print("\n-- a tick on the Stellar row makes STELLAR the incumbent --")
d = only(decide([dog(2, "1.66.10", "$8.00", chosen=""),
                 stellar(3, "1.66.10", "$10.00", chosen=TICK, my_price="12", final="13.99")]))
check("the ticked row is the incumbent whatever the supplier", d.incumbent.row, 3)
check("and the cheaper dog row wins it back", d.winner.row, 2)
check("the note reads Stellar first",
      writes_of(d, "changed"), [(2, "↔ ספק: Stellar → esim.dog ($10.00 → $8.00)")])

print("\n-- a one-supplier SKU is not a choice: untouched --")
check("Stellar only", decide([stellar(2, "1.66.10", "$5.00")]), [])
check("esim.dog only", decide([dog(2, "1.66.10", "$5.00")]), [])
check("a row with no SKU dot is ignored", decide([dog(2, "header", "$5.00"),
                                                  dog(3, "header", "$4.00")]), [])

print("\n-- a switch carries the customer side of the SKU --")
d = only(decide([dog(2, "1.66.10", "$10.00", my_price="15.96", fee="1.2",
                     final="16.99", discount="10%"),
                 stellar(3, "1.66.10", "$8.00")]))
check("U,S,T,V all land on the winner",
      [(k, t) for _, k, t in d.writes if k in CARRY],
      [("final", "16.99"), ("my_price", "15.96"), ("fee", "1.2"), ("discount", "10%")])
check("...on the winner's row", {row for row, k, _ in d.writes if k in CARRY}, {3})

print("\n-- the carry gate: U has to be a price the site can charge --")
check("a plain decimal", final_usd("16.99"), 16.99)
check("a number cell", final_usd(16.99), 16.99)
check("a leading $ is allowed", final_usd("$16.99"), 16.99)
check("an em dash is not a price", final_usd("\u2014"), None)
check("blank is not a price", final_usd(""), None)
check("prose is not a price", final_usd("\u05dc\u05e9\u05d0\u05d5\u05dc \u05d0\u05ea \u05d3\u05e0\u05d9"), None)
check("half a price is not a price", final_usd("16.99 + fee"), None)

# The ticked row IS what the site sells. Moving that tick onto a row with no
# customer price does not save 20% - it takes the package off sale, because
# rowToPackage_ drops a row whose price does not parse.
d = only(decide([dog(2, "1.66.10", "$10.00", my_price="15.96", fee="1.2",
                     final="", discount="10%"),
                 stellar(3, "1.66.10", "$8.00")]))
check("a ticked incumbent with a blank U is SKIPPED, not switched", d.action, "skip")
check("...and says which cell", "U unreadable" in d.reason, True)
check("...and writes nothing at all", d.writes, [])
check("...and colours nothing", d.colours, [])

d = only(decide([dog(2, "1.66.10", "$10.00", my_price="15.96", final="\u2014"),
                 stellar(3, "1.66.10", "$8.00")]))
check("an unparseable U is skipped the same way", d.action, "skip")

# No tick means the site was not selling this row's price anyway: the switch
# goes ahead and simply carries nothing.
d = only(decide([dog(2, "1.66.10", "$10.00", chosen="", my_price="15.96", final=""),
                 stellar(3, "1.66.10", "$8.00")]))
check("an UNTICKED incumbent with no U still switches", d.action, "switch")
check("...carrying nothing", [k for _, k, _ in d.writes if k in CARRY], [])
check("...and the tick lands", writes_of(d, "chosen"), [(3, TICK)])

print("\n-- רווח, in the scraper's own format --")
check("the format", profit_text(5.40, 3.08), "🟢 +$2.32 (+75.3%)")
check("a loss is red and signed", profit_text(1.00, 2.00), "🔴 -$1.00 (-50.0%)")

# 0.6% cheaper: not enough to move the SKU, so the Stellar row stays a
# challenger — and still gets the number the owner needs to see it.
d = only(decide([dog(2, "1.66.10", "$3.08", my_price="5.40"),
                 stellar(3, "1.66.10", "$3.06 (€2.63)", my_price="")]))
check("the untouched Stellar row gets a P off the SKU's S",
      writes_of(d, "profit"), [(3, "🟢 +$2.34 (+76.5%)")])
check("...and this SKU did not switch", d.action, "keep")

d = only(decide([dog(2, "1.66.10", "$3.08", my_price="5.40", profit=""),
                 stellar(3, "1.66.10", "$2.48", profit="already there")]))
check("a P that is already filled in is left alone",
      writes_of(d, "profit"), [(2, "🟢 +$2.32 (+75.3%)")])

d = only(decide([dog(2, "1.66.10", "$3.08", my_price="", profit=""),
                 stellar(3, "1.66.10", "$2.48")]))
check("a blank S writes no P at all", writes_of(d, "profit"), [])

d = only(decide([dog(2, "1.66.10", "$3.08", my_price="5.40"),
                 stellar(3, "1.66.10", "—")]))
check("a row with no cost gets no P", writes_of(d, "profit"), [])

d = only(decide([dog(2, "1.66.10", "$10.00", my_price="15.96", final="16.99"),
                 stellar(3, "1.66.10", "$8.00")]))
check("on a switch the winner's P comes off the SKU's S too",
      writes_of(d, "profit"), [(3, "🟢 +$7.96 (+99.5%)")])

print("\n-- a carried cell keeps the TYPE the sheet gave it --")
# The read is UNFORMATTED, so the Python type IS the sheet's type. Nothing is
# parsed on the way out: a float is written as a number, a str as text, even
# when the str looks like a number.
check("a float is a number", _entered_value(16.99),
      {"userEnteredValue": {"numberValue": 16.99}})
check("an int is a number", _entered_value(12),
      {"userEnteredValue": {"numberValue": 12}})
check("a numeric-looking STRING stays text", _entered_value("16.99"),
      {"userEnteredValue": {"stringValue": "16.99"}})
check("a percent stays text", _entered_value("10%"),
      {"userEnteredValue": {"stringValue": "10%"}})
check("a dollar string stays text", _entered_value("$16.99"),
      {"userEnteredValue": {"stringValue": "$16.99"}})
check("a bool is a bool, not a 1", _entered_value(True),
      {"userEnteredValue": {"boolValue": True}})
check("blank clears the cell", _entered_value(""), {})
check("a zero is still a number, not a clear", _entered_value(0),
      {"userEnteredValue": {"numberValue": 0}})

d = only(decide([dog(2, "1.66.10", "$10.00", my_price=15.96, fee=1.2,
                     final=16.99, discount="10%"),
                 stellar(3, "1.66.10", "$8.00")]))
check("numbers carry as numbers and text as text",
      [(k, v) for _, k, v in d.writes if k in CARRY],
      [("final", 16.99), ("my_price", 15.96), ("fee", 1.2), ("discount", "10%")])
check("a number cell survives every parser on the way",
      eligible(Row(row=2, sku="1.1.1", source="", price="$1.00", validity=30)), True)


print("\n-- a skipped SKU never gets a P either --")
d = only(decide([dog(2, "1.66.10", "$10.00", my_price="15.96"),
                 stellar(3, "1.66.10", "$5.00", chosen=TICK)]))
check("skipped means skipped", d.writes, [])


print("\n-- a BLANK \u05de\u05e7\u05d5\u05e8 is esim.dog, exactly as every other reader says --")
# waverole_sync.gs rowToPackage_ (blank source defaults to esim.dog) and both
# buy bots read a blank source as esim.dog.
# Reading it as "unknown" here made the oldest rows in the sheet ineligible to
# defend themselves, so a Stellar row won them at ANY price.
blank = Row(row=2, sku="1.66.10", source="", price="$1.00", validity="30d",
            chosen=TICK, my_price="5.40", final="5.99")
check("a blank source reads as esim.dog", source_of(blank), "esim.dog")
check("a blank source is eligible", eligible(blank), True)
check("whitespace is blank too", source_of(Row(row=2, source="   ")), "esim.dog")
check("a spelled source is left alone", source_of(Row(row=2, source=" Stellar ")), "Stellar")

d = only(decide([Row(row=2, sku="1.66.10", source="", price="$10.00",
                     validity="30d", chosen=TICK, my_price="15.96", final="16.99"),
                 stellar(3, "1.66.10", "$9.95")]))
check("a blank-source incumbent defends itself at 0.5%", d.action, "keep")
d = only(decide([Row(row=2, sku="1.66.10", source="", price="$10.00",
                     validity="30d", chosen=TICK, my_price="15.96", final="16.99"),
                 stellar(3, "1.66.10", "$8.00")]))
check("...and still loses to a real gap", d.action, "switch")
check("the note names it esim.dog, not an empty string",
      writes_of(d, "changed"), [(3, "\u2194 \u05e1\u05e4\u05e7: esim.dog \u2192 Stellar ($10.00 \u2192 $8.00)")])

# With no tick anywhere, a blank-source row is the esim.dog row and therefore
# the incumbent - the SKU is not ambiguous just because D was never filled in.
d = only(decide([Row(row=2, sku="1.66.10", source="", price="$10.00",
                     validity="30d", my_price="15.96", final="16.99"),
                 stellar(3, "1.66.10", "$9.95")]))
check("a blank-source row is the incumbent when no row is ticked", d.incumbent.row, 2)
d = only(decide([Row(row=2, sku="1.66.10", source="", price="$10.00", validity="30d"),
                 Row(row=3, sku="1.66.10", source="esim.dog", price="$5.00",
                     validity="30d")]))
check("blank + esim.dog is TWO dog rows, so the SKU is skipped", d.action, "skip")

print("\n-- a side with no price prints an em dash, never $0.00 --")
check("no price", money(None), "\u2014")
check("a price", money(8.0), "$8.00")
check("free really is $0.00", money(0.0), "$0.00")
check("the note of a switch off a priceless incumbent",
      switch_note(dog(2, "1.66.10", "\u2014"), stellar(3, "1.66.10", "$9.00")),
      "\u2194 \u05e1\u05e4\u05e7: esim.dog \u2192 Stellar (\u2014 \u2192 $9.00)")
d = only(decide([dog(2, "1.66.10", "\u2014"), stellar(3, "1.66.10", "$99.00")]))
check("...and that is what the switch actually writes",
      writes_of(d, "changed"),
      [(3, "\u2194 \u05e1\u05e4\u05e7: esim.dog \u2192 Stellar (\u2014 \u2192 $99.00)")])

print("\n-- the stripe is A..W, the width enforceChoice_ paints --")
check("23 columns, so \u05e0\u05d1\u05d7\u05e8 is the last one painted", LAST_COL, 23)

print("\n-- --max-switches: a phased first run --")
def three_switches():
    rows = []
    for i, sku in enumerate(("1.1.10", "1.2.10", "1.3.10")):
        rows.append(dog(2 + i * 2, sku, "$10.00", final="16.99", my_price="15.96"))
        rows.append(stellar(3 + i * 2, sku, "$8.00"))
    return decide(rows)

ds = three_switches()
check("all three switch when nothing caps them",
      [d.action for d in ds], ["switch"] * 3)
check("...and an unlimited cap changes nothing", cap_switches(three_switches(), 0), 0)
check("...nor does a negative one", cap_switches(three_switches(), -1), 0)

ds = three_switches()
left = cap_switches(ds, 2)
check("2 of 3 land", [d.action for d in ds], ["switch", "switch", "deferred"])
check("...and it says how many were left", left, 1)
check("the deferred SKU writes nothing", ds[2].writes, [])
check("...and is not recoloured either", ds[2].colours, [])
check("the two that landed keep their writes", bool(ds[0].writes and ds[1].writes), True)

ds = three_switches()
check("a cap of 0 switches... is unlimited, not zero", cap_switches(ds, 0), 0)
ds = three_switches()
check("a cap above the count leaves nothing behind", cap_switches(ds, 9), 0)

# A cap counts SWITCHES only: a kept SKU still gets its \u05e8\u05d5\u05d5\u05d7 fills.
kept_and_switch = decide([
    dog(2, "1.1.10", "$3.08", my_price="5.40", final="5.99"),
    stellar(3, "1.1.10", "$3.06", profit=""),
    dog(4, "1.2.10", "$10.00", my_price="15.96", final="16.99"),
    stellar(5, "1.2.10", "$8.00"),
])
check("two SKUs, one keep one switch",
      [d.action for d in kept_and_switch], ["keep", "switch"])
check("...and unlimited holds nothing back", cap_switches(kept_and_switch, 0), 0)

kept_and_switch2 = decide([
    dog(2, "1.1.10", "$3.08", my_price="5.40", final="5.99"),
    stellar(3, "1.1.10", "$3.06", profit=""),
    dog(4, "1.2.10", "$10.00", my_price="15.96", final="16.99"),
    stellar(5, "1.2.10", "$8.00"),
])
check("a cap of 1 lets the one switch through",
      (cap_switches(kept_and_switch2, 1), [d.action for d in kept_and_switch2]),
      (0, ["keep", "switch"]))
check("the kept SKU still fills \u05e8\u05d5\u05d5\u05d7 under a cap",
      writes_of(kept_and_switch2[0], "profit"), [(3, profit_text(5.40, 3.06))])

if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall chooser tests passed")
sys.exit(0)
