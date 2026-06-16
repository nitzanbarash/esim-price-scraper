#!/bin/bash
# Daily eSIM price check - run by cron
cd "/Users/bhmis/Documents/price update sheets" || exit 1
echo "===== Run started: $(date) =====" >> price_check.log
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 esim_price_scraper.py >> price_check.log 2>&1
echo "===== Run finished: $(date) =====" >> price_check.log
echo "" >> price_check.log
