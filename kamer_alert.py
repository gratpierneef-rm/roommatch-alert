"""
Kamer-alert voor ROOM, Pararius en Kamernet (Delft).

Stuurt één mail met alle NIEUWE woningen van maximaal 650 euro huur.

Gebruik:
    python kamer_alert.py          # normale check
    python kamer_alert.py --debug  # laat per site zien wat er gevonden wordt (geen mail)

Omgevingsvariabelen:
    EMAIL_FROM      je Gmail-adres
    EMAIL_PASSWORD  Gmail app-wachtwoord
    EMAIL_TO        ontvanger(s), komma-gescheiden
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

# ===================== Instellingen =====================
CITY = "Delft"
MAX_RENT = 650.0
SEEN_FILE = "seen.json"

# Zoekpagina's.
# Je kunt deze URL's vervangen door je eigen gefilterde URL uit de browser.
PARARIUS_URLS = [
    "https://www.pararius.nl/huurwoningen/delft/0-650",
]
KAMERNET_URLS = [
    "https://kamernet.nl/huren/huurwoningen-delft",
]

ROOM_API = "https://www.room.nl/portal/object/frontend/getallobjects/format/json"
ROOM_DETAIL = "https://www.room.nl/aanbod/studentenwoningen/details/{}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (persoonlijke kamer-alert)",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
DEBUG = "--debug" in sys.argv
# ========================================================


# ---------- hulpfuncties ----------
def to_float(v):
    try:
        return float(str(v).replace(".", "").replace(",", ".")) if isinstance(v, str) and "," in v \
            else float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_euro(text):
    """'€ 1.250 per maand' -> 1250.0"""
    m = re.search(r"€\s*([\d\.]+)(?:,(\d{2}))?", text)
    if not m:
        return None
    whole = m.group(1).replace(".", "")
    return float(f"{whole}.{m.group(2) or '00'}")


def first(obj, *keys, default=None):
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def estimate_persons(kind, text):
    """Schat het aantal personen: kamer/studio = 1, appartement/huis = aantal slaapkamers."""
    kind = kind.lower()
    if kind in ("kamer", "room", "studio"):
        return 1
    m = re.search(r"(\d+)\s*slaapkamer", text, re.I) or re.search(r"(\d+)\s*bedroom", text, re.I)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"(\d+)\s*kamers?\b", text, re.I) or re.search(r"(\d+)\s*rooms?\b", text, re.I)
    if m:
        return max(1, int(m.group(1)) - 1)  # 'kamers' is inclusief woonkamer
    return 1


def make_item(source, uid, title, rent, persons, area, link, extra="", estimated=False):
    return {
        "source": source, "id": f"{source}:{uid}", "title": title, "rent": rent,
        "persons": persons, "area": area, "link": link, "extra": extra, "estimated": estimated,
    }


def card_text(a_tag):
    """Loop omhoog vanaf de link tot een blok met een €-prijs erin (de advertentiekaart)."""
    node = a_tag
    for _ in range(6):
        txt = node.get_text(" ", strip=True)
        if "€" in txt and len(txt) < 1500:
            return txt
        if node.parent is None:
            break
        node = node.parent
    return a_tag.get_text(" ", strip=True)


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} voor {url} (mogelijk geblokkeerd), overgeslagen.")
        return None
    return r.text


# ---------- ROOM ----------
def room_listings():
    r = requests.get(ROOM_API, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}, timeout=30)
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        raise RuntimeError(f"GET gaf HTTP {r.status_code}, begin van antwoord: {r.text[:150]!r}")
    data = r.json()
    items = []
    for o in data.get("result", []):
        city = o.get("city")
        city = city.get("name", "") if isinstance(city, dict) else str(city or "")
        if CITY.lower() not in city.lower():
            continue
        rent = to_float(first(o, "totalRent", "grossRent", "netRent"))
        if rent is None:
            continue
        n = to_float(first(o, "maximumHouseholdSize", "maxHouseholdSize", "maximumNumberOfPersons",
                           "maxNumberOfPersons", "numberOfPersons"))
        persons = int(n) if n and n >= 1 else 1
        soort = o.get("dwellingType", {})
        soort = soort.get("localizedName", "") if isinstance(soort, dict) else str(soort or "")
        model = o.get("model", {})
        model = model.get("modelCategorie", {}).get("code", "") if isinstance(model, dict) else ""
        title = f"{o.get('street', '')} {o.get('houseNumber', '')}{o.get('houseNumberAddition') or ''} ({soort})".strip()
        extra = f"Reageren tot: {o.get('closingDate', '?')}" + (f" | Model: {model}" if model else "")
        items.append(make_item("ROOM", o.get("id"), title, rent, persons, o.get("areaDwelling"),
                               ROOM_DETAIL.format(o.get("urlKey", o.get("id"))), extra))
    if DEBUG and data.get("result"):
        print("  Voorbeeld ROOM-object:", json.dumps(data["result"][0], ensure_ascii=False)[:800])
    return items


# ---------- Pararius ----------
PARARIUS_LINK = re.compile(r"^/(kamer|appartement|studio|huis)-te-huur/[^/]+/([^/]+)/")


def pararius_listings():
    items = {}
    for url in PARARIUS_URLS:
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = PARARIUS_LINK.match(a["href"])
            if not m or m.group(2) in items:
                continue
            kind, uid = m.group(1), m.group(2)
            text = card_text(a)
            rent = parse_euro(text)
            if rent is None:
                continue
            area = re.search(r"(\d+)\s*m²", text)
            title = a.get_text(" ", strip=True) or a["href"].rstrip("/").split("/")[-1].replace("-", " ").title()
            items[uid] = make_item("Pararius", uid, title, rent, estimate_persons(kind, text),
                                   area.group(1) if area else "?",
                                   "https://www.pararius.nl" + a["href"], estimated=kind not in ("kamer", "studio"))
        time.sleep(3)
    return list(items.values())


# ---------- Kamernet ----------
KAMERNET_LINK = re.compile(r"/(?:en/)?(?:huren|for-rent)/(kamer|studio|appartement|huis|room|apartment|house)-[^/]+/[^/]+/[a-z\-]+-(\d{5,})")


def kamernet_listings():
    items = {}
    for url in KAMERNET_URLS:
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = KAMERNET_LINK.search(a["href"])
            if not m or m.group(2) in items:
                continue
            kind, uid = m.group(1), m.group(2)
            text = card_text(a)
            rent = parse_euro(text)
            if rent is None:
                continue
            area = re.search(r"(\d+)\s*m²", text)
            link = a["href"] if a["href"].startswith("http") else "https://kamernet.nl" + a["href"]
            street = link.rstrip("/").split("/")[-2].replace("-", " ").title()
            items[uid] = make_item("Kamernet", uid, f"{kind.capitalize()} {street}", rent,
                                   estimate_persons(kind, text), area.group(1) if area else "?", link,
                                   estimated=kind not in ("kamer", "studio", "room"))
        time.sleep(3)
    return list(items.values())


# ---------- mail ----------
def describe(it):
    lines = [
        f"[{it['source']}] {it['title']}",
        f"  Huur: €{it['rent']:.2f}",
        f"  Oppervlakte: {it['area'] or '?'} m²" + (f"  |  {it['extra']}" if it["extra"] else ""),
        f"  {it['link']}",
    ]
    return "\n".join(lines) + "\n"


def send_mail(subject, body):
    sender = os.environ["EMAIL_FROM"].strip()
    password = os.environ["EMAIL_PASSWORD"].replace(" ", "").strip()  # app-wachtwoord zonder spaties
    receivers = [e.strip() for e in os.environ["EMAIL_TO"].split(",") if e.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(receivers)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, password)
        s.sendmail(sender, receivers, msg.as_string())


# ---------- main ----------
def main():
    try:
        with open(SEEN_FILE) as f:
            seen = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = set()

    all_items = []
    for name, func in (("ROOM", room_listings), ("Pararius", pararius_listings), ("Kamernet", kamernet_listings)):
        try:
            found = func()
            print(f"{name}: {len(found)} woningen gevonden")
            all_items += found
        except Exception as e:  # één kapotte site mag de rest niet stoppen
            print(f"{name}: fout - {e}")

    if DEBUG:
        for it in all_items[:15]:
            print(describe(it))
        return

    matches = []
    for it in all_items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        if it["rent"] <= MAX_RENT:
            matches.append(it)

    if matches:
        matches.sort(key=lambda x: x["rent"])
        send_mail(f"🏠 {len(matches)} nieuwe kamer(s)/woning(en) in {CITY}",
                  "Nieuwe woningen tot €650:\n\n" + "\n".join(describe(m) for m in matches))
        print(f"Mail verstuurd met {len(matches)} woning(en).")
    else:
        print("Geen nieuwe passende woningen.")

    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


if __name__ == "__main__":
    main()
