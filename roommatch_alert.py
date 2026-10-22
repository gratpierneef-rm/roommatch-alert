#!/usr/bin/env python3
import json, os, smtplib, sys, urllib.request
from email.mime.text import MIMEText
from pathlib import Path

API = "https://roommatching-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
DETAIL = "https://www.roommatch.nl/aanbod/studentenwoningen/details/"
SEEN_FILE = Path(__file__).with_name("seen.json")

CITIES = {c.strip().lower() for c in os.getenv("CITIES", "Delft").split(",") if c.strip()}
MAX_RENT = float(os.getenv("MAX_RENT") or 650)
HOUSEHOLD = int(os.getenv("HOUSEHOLD_SIZE") or 1)


def fetch_all():
    listings, page = [], 0
    while True:
        url = f"{API}?limit=60&locale=nl_NL&page={page}&sort=-publicationDate"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (personal RoomMatch alert)",
            "Accept": "application/json",
            "Referer": "https://www.roommatch.nl/",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        listings += data.get("data", [])
        page += 1
        if page >= data.get("_metadata", {}).get("page_count", 1) or page > 20:
            return listings


def matches(d):
    city = ((d.get("city") or {}).get("name") or "").lower()
    if CITIES and city not in CITIES:
        return False
    rent = d.get("totalRent")
    if rent is not None and rent > MAX_RENT:
        return False
    lo, hi = d.get("minimumHouseholdSize"), d.get("maximumHouseholdSize")
    if lo is not None and HOUSEHOLD < lo:
        return False
    if hi is not None and HOUSEHOLD > hi:
        return False
    return True


def describe(d):
    addr = " ".join(x for x in [d.get("street"), d.get("houseNumber"), d.get("houseNumberAddition")] if x)
    city = (d.get("city") or {}).get("name", "")
    kind = (d.get("woningsoort") or {}).get("localizedNaam", "")
    furn = (d.get("dwellingType") or {}).get("localizedName", "")
    model = (d.get("model") or {}).get("code", "")
    rent = f"€{d['totalRent']:.2f} total/month" if d.get("totalRent") else "?"
    return "\n".join([
        f"{addr}, {city}",
        f"  Rent: {rent}",
        f"  Size: {d.get('areaDwelling')} m²  |  {kind}  |  {furn}",
        f"  Type: {model}  |  reactions so far: {d.get('numberOfReactions')}",
        f"  Available from: {(d.get('availableFromDate') or '?')[:10]}  |  closes: {(d.get('closingDate') or '?')[:10]}",
        f"  {DETAIL}{d.get('urlKey', d.get('id'))}",
    ])


def send_email(subject, body):
    user = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.getenv("MAIL_TO") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def main():
    if os.getenv("TEST_EMAIL") == "true":
        send_email("RoomMatch alert: test email",
                   f"It works! Alerts for {', '.join(sorted(CITIES))} up to €{MAX_RENT:.0f} for {HOUSEHOLD} person(s).")
        print("Test email sent.")
    try:
        listings = fetch_all()
    except Exception as e:
        print(f"WARNING: could not fetch listings: {e}")
        return
    first_run = not SEEN_FILE.exists()
    seen = set() if first_run else set(json.loads(SEEN_FILE.read_text()))
    hits = [d for d in listings if matches(d)]
    new = [d for d in hits if str(d["id"]) not in seen]
    print(f"{len(listings)} listings, {len(hits)} match, {len(new)} new.")
    if new:
        subject = (f"RoomMatch alert is running — {len(new)} current match(es): " if first_run
                   else f"RoomMatch: {len(new)} new listing(s) — ")
        subject += ", ".join(f"{d.get('street')} €{d.get('totalRent', 0):.0f}" for d in new[:3])
        body = "\n\n".join(describe(d) for d in new)
        send_email(subject, body + "\n\nReact fast: https://www.roommatch.nl/aanbod/studentenwoningen")
        print("Email sent.")
    seen |= {str(d["id"]) for d in hits}
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:], indent=0))


if __name__ == "__main__":
    sys.exit(main())
