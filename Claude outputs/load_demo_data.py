"""Load SmartSME_Demo_Dataset.txt into a running SmartSME, the way a person would.

Every purchase, sale and expense goes through Smart Input (parse, then publish),
payments go through Record payment, and cancellations, write-offs and the
deadline change use the same endpoints as their buttons. Nothing touches the
database directly.

Only the Python standard library is needed. Usage:

    python load_demo_data.py --business A --email shopA@example.com --password demo1234
    python load_demo_data.py --business B --email shopB@example.com --password demo1234

Options:
    --file      path to SmartSME_Demo_Dataset.txt (default: next to this script)
    --url       backend address (default: http://localhost:8000)
    --use-ai    allow loading while the backend has a Gemini key (slow: about 15 notes a minute)

If it stops part-way, fix the cause and run the same command again: it keeps a
progress file (demo_load_progress_A.json / _B.json) and carries on from where it
stopped instead of entering anything twice.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


# ---------------------------------------------------------------------------
# Reading the dataset file
# ---------------------------------------------------------------------------
def iso(text: str) -> str:
    d, m, y = text.split()
    return f"{int(y):04d}-{MONTHS[m]:02d}-{int(d):02d}"


def money(text: str) -> float:
    return float(text.replace(",", ""))


def read_dataset(path: str, letter: str) -> dict:
    lines = open(path, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"BUSINESS {letter}:"))
    end = next((i for i, l in enumerate(lines) if i > start and
                (l.startswith("BUSINESS ") or l.startswith("END OF DATASET"))), len(lines))
    block = lines[start:end]

    data = {"settings": {}, "products": [], "parties": [], "purchases": [], "sales": [],
            "expenses": [], "pay_in": [], "pay_out": [], "ops": []}
    section = None
    for i, line in enumerate(block):
        head = re.match(rf"^{letter}(\d)\. ", line)
        if head:
            section = int(head[1])
            continue
        s = line.strip()
        if not s or s.startswith("----") or s.startswith("===="):
            continue

        if section == 0:
            m = re.match(r'^Settings: business name "(.+)", address "(.+)",$', s)
            if m:
                nxt = block[i + 1].strip()
                t = re.match(r"^tax rate ([\d.]+)%, invoice prefix (\w+), default payment terms (\d+) days\.$", nxt)
                data["settings"] = {"name": m[1], "address": m[2], "taxRate": float(t[1]),
                                    "invoicePrefix": t[2], "creditDays": int(t[3])}
                continue
            m = re.match(r"^(.+?)\s+unit (\w+)\s+buy ₹\s*([\d,.]+)\s+sell ₹\s*([\d,.]+)\s+"
                         r"opening\s+(\d+)\s+low-stock (\d+)$", s)
            if m:
                data["products"].append({"name": m[1], "unit": m[2], "purchasePrice": money(m[3]),
                                         "sellingPrice": money(m[4]), "stock": int(m[5]),
                                         "lowStockThreshold": int(m[6])})
                continue
            m = re.match(r"^(.+?)\s{2,}(customer|supplier)\s+(\d+) days", s)
            if m:
                data["parties"].append({"name": m[1], "type": m[2], "creditDays": int(m[3])})
        elif section in (1, 2):
            m = re.match(r"^(.+\.)\s+\[([A-Z]+-\d{4})\]$", s)
            if m:
                text, tag = m[1], m[2]
                paid = re.search(r"paid ₹([\d,.]+)/-", text)
                date = re.search(r" on (\d{1,2} [A-Z][a-z]+ \d{4})\.$", text)
                data["purchases" if section == 1 else "sales"].append(
                    {"text": text, "tag": tag, "paid": money(paid[1]) if paid else 0.0,
                     "date": iso(date[1])})
        elif section == 3:
            m = re.match(r"^(Paid|Spent) .*₹([\d,.]+)/-.* on (\d{1,2} [A-Z][a-z]+ \d{4})\.$", s)
            if m:
                data["expenses"].append({"text": s, "amount": money(m[2]), "date": iso(m[3])})
        elif section in (4, 5):
            m = re.match(r"^(?:Received|Paid) ₹([\d,.]+)(?: \((?:part payment|balance)\))? (?:from|to) (.+?) "
                         r"against (?:invoice|purchase) ([A-Z]+-\d{4}) dated .+?, (.+), on "
                         r"(\d{1,2} [A-Z][a-z]+ \d{4})\.$", s)
            if m:
                how = m[4]
                if how == "in cash":
                    mode, ref = "cash", None
                elif how.startswith("by UPI"):
                    mode, ref = "upi", re.search(r"transaction ID (\w+)", how)[1]
                elif how.startswith("by cheque"):
                    mode, ref = "cheque", re.search(r"no\. (\w+)", how)[1]
                else:
                    mode, ref = "bank_transfer", re.search(r"UTR (\w+)", how)[1]
                data["pay_in" if section == 4 else "pay_out"].append(
                    {"text": s, "amount": money(m[1]), "doc": m[3], "mode": mode,
                     "reference": ref, "date": iso(m[5])})
        elif section == 6:
            m = re.match(r"^Cancel invoice ([A-Z]+-\d{4})", s)
            if m:
                data["ops"].append({"kind": "cancel", "doc": m[1], "text": s})
                continue
            m = re.match(r"^Write off \d+ \w+ of (.+?) on .+: (.+)\. \(Products page > Adjust, quantity (-\d+)\.\)$", s)
            if m:
                data["ops"].append({"kind": "adjust", "product": m[1], "note": m[2],
                                    "delta": int(m[3]), "text": s})
                continue
            m = re.match(r"^Extend the payment deadline of invoice ([A-Z]+-\d{4}) .* to "
                         r"(\d{1,2} [A-Z][a-z]+ \d{4}):", s)
            if m:
                data["ops"].append({"kind": "deadline", "doc": m[1], "date": iso(m[2]), "text": s})
    return data


# ---------------------------------------------------------------------------
# Talking to SmartSME
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None, ok=(200, 201)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=120) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            sys.exit(f"\nCannot reach SmartSME at {self.base} ({e.reason}). Is the backend running?")
        payload = json.loads(raw) if raw else {}
        if status not in ok:
            raise ApiError(status, payload.get("detail", payload))
        return payload

    def all_rows(self, path: str) -> list:
        rows, page = [], 1
        while True:
            res = self.call("GET", f"{path}?page={page}&pageSize=200")
            rows += res["rows"]
            if len(res["rows"]) < 200:
                return rows
            page += 1


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class Progress:
    def __init__(self, path: str):
        self.path = path
        self.done = set(json.load(open(path))) if os.path.exists(path) else set()

    def __contains__(self, key):
        return key in self.done

    def mark(self, key):
        self.done.add(key)
        json.dump(sorted(self.done), open(self.path, "w"))


def stop(what: str, err) -> None:
    sys.exit(f"\nSTOPPED at: {what}\nReason: {err}\n"
             "Nothing after this line was entered. Fix the cause and run the same command again; "
             "it will carry on from here.")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def main():
    # Windows consoles may not be able to print ₹ or Kannada; never crash over that.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Load the SmartSME demo dataset.")
    ap.add_argument("--business", required=True, choices=["A", "B"])
    ap.add_argument("--email", required=True, help="owner login for this business (new or existing)")
    ap.add_argument("--password", required=True, help="at least 6 characters")
    ap.add_argument("--file", default=os.path.join(here, "SmartSME_Demo_Dataset.txt"))
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--use-ai", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"Dataset file not found: {args.file}")
    data = read_dataset(args.file, args.business)
    counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    print(f"Business {args.business}: {data['settings'].get('name')}")
    print("Read from file:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    progress = Progress(os.path.join(here, f"demo_load_progress_{args.business}.json"))
    api = Api(args.url)

    # ---- Account -----------------------------------------------------------
    try:
        api.call("POST", "/api/auth/sign-up", {"businessName": data["settings"]["name"], "name": "Owner",
                                               "email": args.email, "password": args.password})
        print(f"Created a new business and owner account for {args.email}.")
    except ApiError as e:
        if e.status != 409:
            stop("creating the account", e)
        try:
            api.call("POST", "/api/auth/sign-in", {"email": args.email, "password": args.password})
        except ApiError as e2:
            stop("signing in", e2)
        print(f"Signed in as {args.email}.")

    status = api.call("GET", "/api/input/status")
    if status["hasAI"] and not args.use_ai:
        sys.exit("\nThis backend has an AI key configured, so every note would go to Gemini (about 15 a "
                 "minute on the free tier, so over 40 minutes per business).\nEither blank GOOGLE_API_KEY "
                 "in backend/.env and restart the backend (recommended; the dataset was tested this way), "
                 "or run again with --use-ai.")
    delay = 4.2 if status["hasAI"] else 0.0

    already = api.call("GET", "/api/sales?page=1&pageSize=1")["page"]
    has_data = (already.get("total") or 0) > 0
    if has_data and not progress.done:
        sys.exit("\nThis business already has sales in it, and there is no progress file from an earlier "
                 "run.\nUse a new email address so the demo data goes into a fresh business.")

    # ---- Settings, products, parties ----------------------------------------
    if "settings" not in progress:
        current = api.call("GET", "/api/settings")["business"]
        current.update(data["settings"])
        try:
            api.call("PUT", "/api/settings", current)
        except ApiError as e:
            stop("saving Settings", e)
        progress.mark("settings")
    have = {p["name"] for p in api.call("GET", "/api/input/status")["products"]}
    for p in data["products"]:
        if p["name"] not in have:
            try:
                api.call("POST", "/api/products", p)
            except ApiError as e:
                stop(f"adding product {p['name']}", e)
    have = {p["name"] for p in api.call("GET", "/api/input/status")["parties"]}
    for p in data["parties"]:
        if p["name"] not in have:
            try:
                api.call("POST", "/api/parties", p)
            except ApiError as e:
                stop(f"adding party {p['name']}", e)
    print("Setup done: settings, 24 products, parties.")

    # ---- Smart Input: purchases, sales, expenses ----------------------------
    def smart_input(row, kind, key):
        if key in progress:
            return
        if delay:
            time.sleep(delay)
        try:
            draft = api.call("POST", "/api/input/parse-text", {"text": row["text"]})["draft"]
        except ApiError as e:
            stop(row["text"], e)
        problems = []
        if draft["suggestedType"] != kind:
            problems.append(f"read as a {draft['suggestedType']}, expected a {kind}")
        if draft.get("date") != row["date"]:
            problems.append(f"date read as {draft.get('date')}, expected {row['date']}")
        if kind == "expense":
            if abs((draft.get("amount") or 0) - row["amount"]) > 0.005:
                problems.append(f"amount read as {draft.get('amount')}")
            payload = {"type": "expense", "source": "nlp", "category": draft["category"],
                       "description": draft["note"], "amount": draft["amount"], "date": draft["date"]}
        else:
            walk_in = "walk-in customer" in row["text"] or row["text"].startswith("A customer ")
            if not draft["partyId"] and not walk_in:
                problems.append(f"party \"{draft['partyName']}\" not matched to a saved party")
            unmatched = [i["description"] for i in draft["items"] if not i["productId"]]
            if unmatched:
                problems.append(f"products not matched: {unmatched}")
            payload = {"type": kind, "source": "nlp", "partyId": draft["partyId"], "items": draft["items"],
                       "amountPaid": row["paid"], "discountType": draft["discountType"],
                       "discountValue": draft["discountValue"], "date": draft["date"]}
        if problems:
            stop(row["text"], "; ".join(problems))
        try:
            res = api.call("POST", "/api/input/publish", payload)
        except ApiError as e:
            stop(row["text"], e)
        if kind != "expense" and row["tag"] not in res.get("ok", ""):
            progress.mark(key)
            stop(row["text"], f"SmartSME numbered it differently ({res.get('ok')}), expected {row['tag']}. "
                 "The business probably had other entries in it. Start again with a new email address.")
        progress.mark(key)

    for label, kind, rows in (("purchases", "purchase", data["purchases"]),
                              ("sales", "sale", data["sales"]),
                              ("expenses", "expense", data["expenses"])):
        for n, row in enumerate(rows, 1):
            smart_input(row, kind, f"{kind}:{row.get('tag', n)}")
            print(f"\r{label}: {n}/{len(rows)}", end="", flush=True)
        print()

    # ---- Payments -------------------------------------------------------------
    sale_id = {r["invoiceNumber"]: r["id"] for r in api.all_rows("/api/sales")}
    pur_id = {r["referenceNumber"]: r["id"] for r in api.all_rows("/api/purchases")}
    for label, rows, ids, path in (("customer payments", data["pay_in"], sale_id, "sales"),
                                   ("supplier payments", data["pay_out"], pur_id, "purchases")):
        for n, row in enumerate(rows, 1):
            key = f"{path}-pay:{n}:{row['doc']}:{row['date']}"
            if key not in progress:
                try:
                    api.call("POST", f"/api/{path}/{ids[row['doc']]}/payment",
                             {"amount": row["amount"], "date": row["date"], "mode": row["mode"],
                              "reference": row["reference"]})
                except (ApiError, KeyError) as e:
                    stop(row["text"], e)
                progress.mark(key)
            print(f"\r{label}: {n}/{len(rows)}", end="", flush=True)
        print()

    # ---- Cancellations, write-offs, deadline ----------------------------------
    products = {p["name"]: p["id"] for p in api.call("GET", "/api/input/status")["products"]}
    for n, op in enumerate(data["ops"], 1):
        key = f"op:{n}"
        if key in progress:
            continue
        try:
            if op["kind"] == "cancel":
                api.call("POST", f"/api/sales/{sale_id[op['doc']]}/cancel")
            elif op["kind"] == "adjust":
                api.call("POST", f"/api/products/{products[op['product']]}/adjust",
                         {"delta": op["delta"], "note": op["note"]})
            else:
                api.call("PATCH", f"/api/sales/{sale_id[op['doc']]}/due-date", {"date": op["date"]})
        except (ApiError, KeyError) as e:
            stop(op["text"], e)
        progress.mark(key)
    print(f"cancellations, write-offs and deadline changes: {len(data['ops'])}")

    # ---- Result --------------------------------------------------------------
    h = api.call("GET", "/api/dashboard")["health"]
    print(f"\nDone. Business health now: {h.get('overall')}  (cash flow {h.get('cashFlow')}, "
          f"profitability {h.get('profitability')}, revenue {h.get('revenue')}, inventory {h.get('inventory')})")
    print(f"Sign in as {args.email} to see the dashboard.")


if __name__ == "__main__":
    main()
