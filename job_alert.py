"""
BD Govt Job Alert -> Telegram
Checks Bangladesh government job circular websites and sends new
circulars to you through your own Telegram bot.
"""
import html
import json
import os
import time
import urllib.parse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# ---------------------------------------------------------------
# Websites to watch. Add or remove sites here.
#   filter=True  -> only links whose text matches KEYWORDS count
#   filter=False -> every new link on the page counts as a new job
# ---------------------------------------------------------------
SOURCES = [
    {"name": "Teletalk All Jobs", "url": "https://alljobs.teletalk.com.bd/", "filter": False},
    {"name": "BPSC", "url": "http://www.bpsc.gov.bd/", "filter": True},
    {"name": "Bangladesh Bank", "url": "https://erecruitment.bb.org.bd/", "filter": False},
    {"name": "Bangladesh Army", "url": "https://joinbangladesharmy.army.mil.bd/", "filter": True},
]

KEYWORDS = [
    "circular", "recruitment", "job", "vacancy", "notice", "advertisement",
    "নিয়োগ", "বিজ্ঞপ্তি", "নিয়োগ", "শূন্য পদ", "চাকরি",
]

SEEN_FILE = "seen.json"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (BD-Job-Alert)"}


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def fetch(url):
    # Many .gov.bd sites have broken SSL certificates, so retry without verification.
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
    except requests.exceptions.SSLError:
        r = requests.get(url, headers=HEADERS, timeout=40, verify=False)
    r.raise_for_status()
    return r.text


def extract_links(source):
    soup = BeautifulSoup(fetch(source["url"]), "html.parser")
    jobs = {}
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"].strip()
        if len(text) < 6 or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if source["filter"] and not any(k.lower() in text.lower() for k in KEYWORDS):
            continue
        full = urllib.parse.urljoin(source["url"], href)
        jobs[full] = text[:150]
    return jobs


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[no Telegram keys set] would send:\n" + message)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=data, timeout=40)
    print("Telegram:", r.status_code, "" if r.ok else r.text)
    time.sleep(1)  # stay well under Telegram's rate limit


def main():
    seen = load_seen()
    new_jobs = []

    for src in SOURCES:
        try:
            links = extract_links(src)
        except Exception as e:
            print(f"[{src['name']}] failed: {e}")
            continue

        old = set(seen.get(src["name"], []))
        first_run = src["name"] not in seen
        fresh = [(u, t) for u, t in links.items() if u not in old]
        print(f"[{src['name']}] {len(links)} links, {len(fresh)} new")

        # First run: just remember what's there, don't spam old circulars.
        if not first_run:
            new_jobs += [(src["name"], t, u) for u, t in fresh]

        # Keep the list from growing forever.
        seen[src["name"]] = (list(old) + [u for u, _ in fresh])[-2000:]

    # Send in batches (Telegram allows up to 4096 characters per message).
    for i in range(0, len(new_jobs), 10):
        batch = new_jobs[i:i + 10]
        lines = ["🇧🇩 <b>New Govt Job Circular</b>", ""]
        for name, title, url in batch:
            lines += [
                f"📌 {html.escape(title)}",
                f"🏢 {html.escape(name)}",
                f'🔗 <a href="{html.escape(url, quote=True)}">Open circular</a>',
                "",
            ]
        send_telegram("\n".join(lines))

    save_seen(seen)
    print(f"Done. {len(new_jobs)} new circular(s) sent.")


if __name__ == "__main__":
    main()
