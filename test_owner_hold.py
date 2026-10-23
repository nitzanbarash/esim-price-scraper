#!/usr/bin/env python3
"""The owner's hold in 'במלאי/רווחי' survives the morning scrape — 2026-10-23.

The owner took Asia 1GB (1.0A.1) off sale by hand: "שפשוט ירד מהמלאי בינתיים".
Any word in the stock cell takes a package off the site, but this run rewrote
the cell on every row it priced (empty when it pays), so a hand-typed word
lasted until the next morning. choose_supplier.py, stellar_prices.py and the
sheet's applyFee_ already keep an owner's word; a cell that starts with
OWNER_HOLD_LABEL is now kept here too, whatever the row costs.

Run:  python test_owner_hold.py
"""
from esim_price_scraper import (BELOW_MIN_LABEL, OVER_CEILING_LABEL,
                                OWNER_HOLD_LABEL, UNPROFITABLE_LABEL,
                                drop_held_stock_writes, owner_hold)


def test_the_hold_is_recognised():
    assert owner_hold(OWNER_HOLD_LABEL)
    assert owner_hold(f"  {OWNER_HOLD_LABEL}  ")
    assert owner_hold(f"{OWNER_HOLD_LABEL} — עד שנחליט על מחיר")
    assert owner_hold("‏" + OWNER_HOLD_LABEL)          # a bidi mark from a paste


def test_the_bots_own_words_are_not_a_hold():
    for word in ("", None, "לא במלאי", BELOW_MIN_LABEL, OVER_CEILING_LABEL,
                 UNPROFITABLE_LABEL, "לא רווחי — מעל טווח"):
        assert not owner_hold(word), word


def test_a_hold_typed_mid_run_drops_only_that_stock_write():
    """flush() re-reads the stock column and drops the bot's write to a cell
    the owner held since the run began; everything else still goes out."""
    w = lambda rng: {'range': rng, 'values': [['x']]}
    updates = [w('Q2'), w('G2'), w('J2'), w('Q3'), w('QA2'), w('Q22')]
    kept = [u['range'] for u in drop_held_stock_writes(updates, 'Q', {2})]
    assert kept == ['G2', 'J2', 'Q3', 'QA2', 'Q22'], kept
    assert len(drop_held_stock_writes(updates, 'Q', set())) == len(updates)


def test_every_stock_write_goes_through_put():
    """The hold is checked in put(); a stock write that bypassed it would
    wipe the owner's word again."""
    import inspect
    import esim_price_scraper
    assert "'old_stock': _get('stock')" in inspect.getsource(esim_price_scraper)
    src = inspect.getsource(esim_price_scraper.ESIMScraper.run)
    assert "if key == 'stock' and row in owner_held:" in src
    allowed = ("put(r, 'stock'", "if key == 'stock' and row in owner_held:",
               "if 'stock' in col:", "letter = col_letter(col['stock'])")
    stray = [l.strip() for l in src.splitlines()
             if "'stock'" in l and not l.strip().startswith(allowed)]
    assert not stray, f"a stock write outside put(): {stray}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} passed")
