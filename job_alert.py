"""
BD Govt Job Alert -> Telegram
Checks Bangladesh government job circular websites and sends new
circulars to you through your own Telegram bot.
"""
import html
import json
import os
import sys
import time
import unicodedata
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
    {"name": "Bangladesh Army", "url": "https://joinbangladesharmy.army.mil.bd/", "filter": True},
]

KEYWORDS = [
    "circular", "recruitment", "job", "vacancy", "notice", "advertisement", "application",
    "নিয়োগ", "বিজ্ঞপ্তি", "বিজ্ঞপ্তি", "শূন্য পদ", "শূন্যপদ", "চাকরি", "আবেদন", "পরীক্ষা", "নোটিশ",
]


def norm(text):
    # Bangla letters like য় can be typed two different ways; make them match.
    return unicodedata.normalize("NFC", text).lower()


NORM_KEYWORDS = [norm(k) for k in KEYWORDS]

SEEN_FILE = "seen.json"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def fetch_simple(url):
    # Many .gov.bd sites have broken SSL certificates, so retry without verification.
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
    except requests.exceptions.SSLError:
        r = requests.get(url, headers=HEADERS, timeout=40, verify=False)
    return r.text, r.status_code


def fetch_browser(url):
    """Open the page in a real (headless) Chrome so JavaScript-built pages load."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], ignore_https_errors=True)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)  # give the job list time to load
        except Exception as e:
            print(f"    browser warning: {e}")
        content = page.content()
        browser.close()
    return content


def parse_links(page_html, source):
    soup = BeautifulSoup(page_html, "html.parser")
    anchors = soup.find_all("a", href=True)
    jobs = {}
    for a in anchors:
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"].strip()
        if len(text) < 6 or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if source["filter"] and not any(k in norm(text) for k in NORM_KEYWORDS):
            continue
        full = urllib.parse.urljoin(source["url"], href)
        jobs[full] = text[:150]
    title = soup.title.get_text(strip=True)[:60] if soup.title else "(no title)"
    return jobs, len(anchors), title, soup


def show_sample(soup):
    """Print what the bot sees, to help fix sites that give 0 job links."""
    texts = []
    for a in soup.find_all("a", href=True):
        t = " ".join(a.get_text(" ", strip=True).split())
        if len(t) >= 6:
            texts.append(t[:60])
    print("    sample links:", " | ".join(texts[:15]) or "(none)")
    body = " ".join(soup.get_text(" ", strip=True).split())
    print("    page text:", body[:300] or "(empty)")


def extract_links(source):
    page_html, status = fetch_simple(source["url"])
    jobs, total, title, soup = parse_links(page_html, source)
    print(f"    simple fetch: HTTP {status}, {len(page_html)} bytes, "
          f"{total} links on page, title: {title}")

    # Page looks empty -> probably built by JavaScript, so use the browser.
    if total < 5:
        page_html = fetch_browser(source["url"])
        jobs, total, title, soup = parse_links(page_html, source)
        print(f"    browser fetch: {len(page_html)} bytes, "
              f"{total} links on page, title: {title}")
    if not jobs:
        show_sample(soup)
    return jobs


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[no Telegram keys set] would send:\n" + message)
        return True
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
    return r.ok


def main():
    seen = load_seen()
    new_jobs = []

    for src in SOURCES:
        print(f"[{src['name']}] {src['url']}")
        try:
            links = extract_links(src)
        except Exception as e:
            print(f"[{src['name']}] failed: {e}")
            continue

        old = set(seen.get(src["name"], []))
        # Nothing saved yet for this site -> treat as first run.
        first_run = not old
        fresh = [(u, t) for u, t in links.items() if u not in old]
        print(f"[{src['name']}] {len(links)} job links, {len(fresh)} new"
              + (" (first run: saving only, no alert)" if first_run and fresh else ""))

        # First run: just remember what's there, don't spam old circulars.
        if not first_run:
            new_jobs += [(src["name"], t, u) for u, t in fresh]

        # Keep the list from growing forever.
        seen[src["name"]] = (list(old) + [u for u, _ in fresh])[-2000:]

    all_sent = True
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
        all_sent = send_telegram("\n".join(lines)) and all_sent

    if not all_sent:
        # Don't save, so these circulars are sent again on the next run.
        print("ERROR: Telegram message failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(1)

    save_seen(seen)
    print(f"Done. {len(new_jobs)} new circular(s) sent.")


if __name__ == "__main__":
    main()
