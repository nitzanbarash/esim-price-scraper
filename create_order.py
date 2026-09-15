#!/usr/bin/env python3
"""Create a manual order on the site -- run by create-order.yml.

Sends {action: create} to waverole.com/api/orders with the ORDERS_TOKEN this
repo holds as a secret, so an order can be created without the token ever
leaving GitHub. The site queues it for the supplier named and wakes the
Stellar buyer at once; that buyer then buys, posts the eSIM and mails the
customer itself.

This repo is public and so is this log. The address is masked before
anything else is printed, the reply's page link (the customer's key to their
eSIM) is masked too, and only the new order id and the buyer's name appear.
"""
import json
import os
import sys
import urllib.error
import urllib.request

SITE = os.environ.get("SITE_ORIGIN", "https://www.waverole.com").rstrip("/")


def env(name):
    return os.environ.get(name, "").strip()


def mask(value):
    if value:
        print(f"::add-mask::{value}", flush=True)


def number(text):
    try:
        return float(text)
    except ValueError:
        sys.exit(f"not a number: {text!r}")


def main():
    token = env("ORDERS_TOKEN")
    if not token:
        sys.exit("ORDERS_TOKEN is not set")
    email = env("CUSTOMER_EMAIL")
    mask(email)

    body = {"action": "create", "sku": env("SKU"), "customer_email": email,
            "lang": env("LANG_CODE") or "he"}
    paid = number(env("PAID_USD") or "0")
    if paid > 0:
        body["paid_usd"] = paid
    supplier = env("SUPPLIER")
    if supplier and supplier != "catalogue":
        body["supplier"] = supplier
    for key, name in (("dest", "DEST"), ("iso", "ISO")):
        if env(name):
            body[key] = env(name)
    for key, name in (("gb", "GB"), ("days", "DAYS")):
        if env(name):
            body[key] = number(env(name))

    req = urllib.request.Request(
        SITE + "/api/orders", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            why = json.load(e).get("error", "")
        except Exception:
            why = ""
        sys.exit(f"the site answered {e.code} {why}".rstrip())

    mask(reply.get("order_url"))
    line = (f"Order {reply.get('order_id', '?')} created and queued for "
            f"{reply.get('supplier', '?')}.")
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    main()
