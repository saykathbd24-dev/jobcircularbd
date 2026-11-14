"""
BD Govt Job Alert -> Telegram
Checks Bangladesh government job circular websites and sends new
circulars to you through your own Telegram bot.

Scheduled run: sends only circulars that are new since the last run.
Manual run (MANUAL_RUN=true): also sends every circular published between
DATE_FROM and DATE_TO (empty = today, Bangladesh time).
For sites with details=True, each circular page is opened and every
post is listed with its own link.
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# ---------------------------------------------------------------
# Websites to watch. Add or remove sites here.
#   filter=True   -> only links whose text matches KEYWORDS count
#   filter=False  -> every new link on the page counts as a new job
#   browser=True  -> always open in headless Chrome (JavaScript-built page)
#   details=True  -> open each circular's page and list its posts
#   type="teletalk" -> alljobs.teletalk.com.bd job cards (see read_teletalk)
# ---------------------------------------------------------------
SOURCES = [
    # alljobs is a React app: no job links, only cards -> special reader.
    {"name": "Teletalk All Jobs", "url": "https://alljobs.teletalk.com.bd/jobs/government",
     "type": "teletalk"},
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


def clean(text):
    return " ".join(text.split())


def tidy(text):
    """Drop a leading serial number and trailing 'Apply'/'Details' words from row text."""
    words = text.split()
    while words and norm(words[-1]) in {"apply", "details", "view", "বিস্তারিত", "আবেদন"}:
        words.pop()
    if len(words) > 1 and words[0].rstrip(".)").translate(BN_DIGITS).isdigit():
        words.pop(0)
    return " ".join(words)


NORM_KEYWORDS = [norm(k) for k in KEYWORDS]

SEEN_FILE = "seen.json"
SEEN_VERSION = 3  # bump when the way circulars are remembered changes
DEBUG_DIR = "debug"  # downloaded pages are saved here (uploaded as a GitHub artifact)
MAX_CIRCULARS = 40   # safety limit per run
DAILY_HOUR = 9       # daily Teletalk summary: first run after this hour (Bangladesh time)
TELETALK_HOME = "https://alljobs.teletalk.com.bd/"
MAX_POSTS = 60

# Link texts that say nothing about the job; for these we use the whole row's text.
GENERIC_TEXTS = {norm(t) for t in [
    "apply", "apply now", "apply online", "details", "view", "view details", "click here",
    "more", "read more", "download", "see more", "বিস্তারিত", "আবেদন করুন", "ডাউনলোড",
]}
# Menu links on circular pages that are not posts.
NAV_TEXTS = {norm(t) for t in [
    "home", "login", "log in", "sign in", "logout", "contact", "contact us", "help", "faq",
    "faqs", "about", "about us", "user manual", "privacy policy", "terms", "terms of use",
    "teletalk", "hotline", "হোম", "যোগাযোগ",
]}
PDF_WORDS = [norm(w) for w in ["circular", "advertisement", "বিজ্ঞপ্তি", "বিজ্ঞপ্তি"]]
INFO_WORDS = [norm(w) for w in [
    "deadline", "last date", "start", "published", "publish", "application", "vacancy",
    "শেষ", "শুরু", "তারিখ",
]]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MANUAL_RUN = os.environ.get("MANUAL_RUN", "").strip().lower() == "true"
BD_TZ = timezone(timedelta(hours=6))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}

# ---------------------------------------------------------------- dates

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})[\s,.-]+(\d{4})\b"), "dMy"),
    (re.compile(r"\b([A-Za-z]{3,9})[\s.-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s-]+(\d{4})\b"), "Mdy"),
]


def date_range():
    """Dates typed in the Run workflow form. Accepts 10-11-2026, 2026-11-10, 10 Nov 2026..."""
    today = datetime.now(BD_TZ).date()

    def read(name):
        s = os.environ.get(name, "").strip()
        if not s:
            return None
        d = find_dates(s)
        if not d:
            print(f"WARNING: could not read {name}='{s}', using today instead.")
            return None
        return d[0]

    start = read("DATE_FROM") or today
    end = read("DATE_TO") or (start if os.environ.get("DATE_FROM", "").strip() else today)
    if start > end:
        start, end = end, start
    return start, end


def published(item):
    """The circular's publish date = earliest date in its row (deadline comes later)."""
    return min(item["dates"]) if item["dates"] else None


def fmt_range(start, end):
    return f"{start:%d %b %Y}" if start == end else f"{start:%d %b %Y} – {end:%d %b %Y}"


def find_dates(text):
    """All dates written in the text (English or Bangla digits), in order, no repeats."""
    text = text.translate(BN_DIGITS)
    found = []
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            a, b, c = m.groups()
            try:
                if kind == "dmy":
                    d, mo, y = int(a), int(b), int(c)
                elif kind == "ymd":
                    y, mo, d = int(a), int(b), int(c)
                elif kind == "dMy":
                    d, mo, y = int(a), MONTHS.get(b[:3].lower()), int(c)
                else:
                    mo, d, y = MONTHS.get(a[:3].lower()), int(b), int(c)
                if not mo:
                    continue
                found.append((m.start(), datetime(y, mo, d).date()))
            except ValueError:
                continue
    out = []
    for _, d in sorted(found):
        if d not in out:
            out.append(d)
    return out

# ---------------------------------------------------------------- storage


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"NOTE: {SEEN_FILE} not found -> every site is treated as a first run.")
        seen = {}
    if seen and seen.get("_version") != SEEN_VERSION:
        print("NOTE: seen.json is from an older version -> starting fresh.")
        seen = {}
    seen["_version"] = SEEN_VERSION
    return seen


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def save_debug(name, page_html):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]
        with open(os.path.join(DEBUG_DIR, safe + ".html"), "w", encoding="utf-8") as f:
            f.write(page_html)
    except OSError as e:
        print(f"    (could not save debug page: {e})")

# ---------------------------------------------------------------- fetching


def fetch_simple(url):
    """Normal download, with retries. Many .gov.bd sites have broken SSL certificates
    or only work on http:// (or only on https://), so all of that is tried."""
    other = (url.replace("https://", "http://", 1) if url.startswith("https://")
             else url.replace("http://", "https://", 1))
    errors = []
    for attempt in range(3):
        for u in (url, other):
            for verify in (True, False):
                try:
                    r = requests.get(u, headers=HEADERS, timeout=40, verify=verify)
                    if r.status_code < 500:
                        if u != url:
                            print(f"    (worked with {u})")
                        return r.text, r.status_code, r.headers.get("Content-Type", ""), r.url
                    errors.append(f"{u}: HTTP {r.status_code}")
                    break
                except requests.exceptions.SSLError as e:
                    errors.append(f"{u}: SSL {str(e)[:80]}")
                    continue  # try again without certificate check
                except requests.exceptions.RequestException as e:
                    errors.append(f"{u}: {type(e).__name__}")
                    break
        time.sleep(5)
    raise RuntimeError("could not download page -> " + "; ".join(errors[-4:]))


def fetch_browser(url, wait_ms=8000):
    """Open the page in a real (headless) Chrome so JavaScript-built pages load."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], ignore_https_errors=True)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(wait_ms)  # give the list time to load
        except Exception as e:
            print(f"    browser warning: {e}")
        content, final_url = page.content(), page.url
        browser.close()
    return content, final_url


def is_pdf_url(url):
    return urllib.parse.urlparse(url).path.lower().endswith(".pdf")

# ---------------------------------------------------------------- main list


def parse_links(page_html, source, base_url=None):
    soup = BeautifulSoup(page_html, "html.parser")
    # Menus, header and footer are never job circulars.
    for tag in soup(["nav", "header", "footer", "script", "style", "noscript"]):
        tag.decompose()
    anchors = soup.find_all("a", href=True)
    jobs = {}
    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = clean(a.get_text(" ", strip=True))
        # Job tables: use the whole row (organisation + post + dates), so that
        # "Apply" buttons and the title link in the same row become one entry.
        row = a.find_parent("tr")
        if row is None and (len(text) < 6 or norm(text) in GENERIC_TEXTS):
            row = a.find_parent("li")
        if row is not None:
            text = clean(row.get_text(" ", strip=True))
        if len(text) < 6:
            continue
        if source["filter"] and not any(k in norm(text) for k in NORM_KEYWORDS):
            continue
        full = urllib.parse.urljoin(base_url or source["url"], href)
        # Remember link + text together: Teletalk often reuses the same link
        # (e.g. an organisation's teletalk subdomain) for every new circular.
        key = f"{full} | {text[:150]}"
        jobs[key] = {"url": full, "text": text[:150], "dates": find_dates(text)}
    title = soup.title.get_text(strip=True)[:60] if soup.title else "(no title)"
    return jobs, len(anchors), title, soup


def show_sample(soup):
    """Print what the bot sees, to help fix sites that give 0 job links."""
    texts = []
    for a in soup.find_all("a", href=True):
        t = clean(a.get_text(" ", strip=True))
        if len(t) >= 6:
            texts.append(t[:60])
    print("    sample links:", " | ".join(texts[:15]) or "(none)")
    body = clean(soup.get_text(" ", strip=True))
    print("    page text:", body[:300] or "(empty)")


def extract_links(source):
    """Try a normal download and (if needed) a real browser; use whichever finds more.
    One method failing never stops the other."""
    results, errors = [], []

    try:
        page_html, status, _, final_url = fetch_simple(source["url"])
        jobs, total, title, soup = parse_links(page_html, source, final_url)
        print(f"    simple fetch: HTTP {status}, {len(page_html)} bytes, "
              f"{total} links on page, {len(jobs)} job links, title: {title}")
        results.append((len(jobs), jobs, soup, page_html))
    except Exception as e:
        print(f"    simple fetch failed: {e}")
        errors.append(f"simple: {e}")
        total = 0

    # Page looks empty (or site is marked browser=True) -> it may be built by
    # JavaScript, so also open it in a real browser.
    if source.get("browser") or total < 5:
        try:
            page_html, final_url = fetch_browser(source["url"])
            jobs, total_b, title, soup = parse_links(page_html, source, final_url)
            print(f"    browser fetch: {len(page_html)} bytes, "
                  f"{total_b} links on page, {len(jobs)} job links, title: {title}")
            results.append((len(jobs), jobs, soup, page_html))
        except Exception as e:
            print(f"    browser fetch failed: {str(e)[:200]}")
            errors.append(f"browser: {str(e)[:100]}")

    if not results:
        raise RuntimeError(" | ".join(errors))
    _, jobs, soup, page_html = max(results, key=lambda r: r[0])
    save_debug("list_" + source["name"], page_html)
    if not jobs:
        show_sample(soup)
    else:
        print("    first few:", " || ".join(j["text"][:70] for j in list(jobs.values())[:5]))
    return jobs

# ---------------------------------------------------------------- alljobs.teletalk

OPENINGS_RX = re.compile(r"openings?\s*:?\s*(\d+)", re.I)


def parse_teletalk_cards(page_html):
    """Each job card = one organisation: name (h4) + 'Openings: N' (h6)."""
    soup = BeautifulSoup(page_html, "html.parser")
    cards = []
    for card in soup.select("div.govt-job-card"):
        h4 = card.find(["h4", "h3", "h5"])
        org = clean(h4.get_text(" ", strip=True)) if h4 else ""
        if not org and card.find("img", alt=True):
            org = clean(card.find("img", alt=True)["alt"])
        if not org:
            continue
        m = OPENINGS_RX.search(card.get_text(" ", strip=True))
        if not m:
            continue  # not a job card
        cards.append({"org": org, "openings": int(m.group(1))})
    # The same card can appear in two sections; keep one.
    out = {}
    for c in cards:
        out.setdefault(c["org"], c)
    return list(out.values())


STAT_ICONS = [("live", "🟢"), ("posted", "🆕"), ("deadline", "⏰"), ("expire", "⌛")]


def parse_teletalk_stats(page_html):
    """The 4 boxes on alljobs: Live Jobs / Posted Today / Deadline Today / Expires in 3 days."""
    soup = BeautifulSoup(page_html, "html.parser")
    stats = {}
    for box in soup.select("div.banner-bottom-icon-item"):
        label, number = box.find("h6"), box.find("h4")
        if not label or not number:
            continue
        digits = re.sub(r"\D", "", number.get_text().translate(BN_DIGITS))
        if digits:
            stats[clean(label.get_text(" ", strip=True))] = int(digits)
    return stats


def daily_summary(seen, today):
    """Once a day: how many jobs are live, posted today, ending today, ending soon."""
    last = seen.get("_daily", {})
    now = datetime.now(BD_TZ)
    due = last.get("date") != today.isoformat() and now.hour >= DAILY_HOUR
    if not (due or MANUAL_RUN):
        return True
    print("[Teletalk daily summary]")
    try:
        page_html, _ = fetch_browser(TELETALK_HOME)
        stats = parse_teletalk_stats(page_html)
    except Exception as e:
        print(f"    summary failed: {str(e)[:150]}")
        return True  # try again on the next run
    if not stats:
        save_debug("teletalk_home", page_html)
        print("    summary: the 4 number boxes were not found on the page")
        return True
    print("    " + ", ".join(f"{k}: {v}" for k, v in stats.items()))

    old = last.get("counts", {})
    lines = [f"📊 <b>Teletalk daily update</b> — {today:%d %b %Y}", ""]
    for label, n in stats.items():
        icon = next((i for k, i in STAT_ICONS if k in label.lower()), "•")
        line = f"{icon} {html.escape(label)}: <b>{n}</b>"
        if "live" in label.lower() and label in old and old[label] != n:
            line += f" ({n - old[label]:+d} since last update)"
        lines.append(line)
    lines += ["", f'🔗 <a href="{TELETALK_HOME}">Open alljobs</a>']
    ok = send_lines(lines)
    if ok and due:
        seen["_daily"] = {"date": today.isoformat(), "counts": stats}
    return ok
    """Keep the site's own data (JSON) so a later version can read it directly."""
    try:
        if len(store) >= 15 or "json" not in response.headers.get("content-type", ""):
            return
        store.append({"url": response.url, "data": response.json()})
    except Exception:
        pass


def read_teletalk(source):
    """Open alljobs in Chrome, load all cards, open every 'See Openings' list."""
    from playwright.sync_api import sync_playwright
    api = []
    posts_by_org = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], ignore_https_errors=True,
                                viewport={"width": 1366, "height": 900})
        page.on("response", lambda r: save_api_json(api, r))
        try:
            page.goto(source["url"], wait_until="domcontentloaded", timeout=90000)
            page.wait_for_selector("div.govt-job-card", timeout=45000)
        except Exception as e:
            print(f"    browser warning: {str(e)[:200]}")
        # Scroll until no new cards appear (the list may load while scrolling).
        last = -1
        for _ in range(30):
            n = page.locator("div.govt-job-card").count()
            if n == last:
                break
            last = n
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(1500)
        print(f"    cards on page: {last}")

        # Click every card's "See Openings" drop-down and read the post names.
        cards = page.locator("div.govt-job-card")
        for i in range(min(cards.count(), 150)):
            card = cards.nth(i)
            try:
                org = clean(card.locator("h4").first.inner_text(timeout=3000))
                box = card.locator("input[role='combobox']").first
                box.click(timeout=5000)
                page.wait_for_timeout(1200)
                opts = page.locator("[id^='react-select-'][id*='-option-']")
                names = [clean(t) for t in opts.all_inner_texts() if clean(t)]
                if names:
                    posts_by_org[org] = names
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception as e:
                if i < 3:
                    print(f"    could not open list on card {i}: {str(e)[:120]}")
        page_html = page.content()
        browser.close()

    save_debug("list_" + source["name"], page_html)
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, "teletalk_api.json"), "w", encoding="utf-8") as f:
            json.dump(api, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"    (could not save api json: {e})")

    cards = parse_teletalk_cards(page_html)
    print(f"    {len(cards)} organisations, post lists opened for {len(posts_by_org)}")
    jobs = {}
    for c in cards:
        org, n = c["org"], c["openings"]
        names = posts_by_org.get(org)
        if names:
            # One key per post: a new post from a known organisation is still new.
            for name in names:
                key = f"teletalk | {org} | {name}"
                jobs[key] = {"url": source["url"], "org": org, "post": name,
                             "text": f"{org} — {name}"[:150], "dates": find_dates(name)}
        else:
            key = f"teletalk | {org} | openings:{n}"
            jobs[key] = {"url": source["url"], "org": org, "post": None, "openings": n,
                         "text": f"{org} (Openings: {n})", "dates": []}
    if jobs:
        print("    first few:", " || ".join(j["text"][:70] for j in list(jobs.values())[:5]))
    return jobs


def teletalk_really_new(fresh, old, links):
    """'Openings: N' keys change when posts expire. Only count it as new when the
    organisation is new or its openings went UP."""
    best = {}
    for k in old:
        parts = k.split(" | ")
        if len(parts) == 3 and parts[2].startswith("openings:"):
            n = int(parts[2].split(":")[1] or 0)
            best[parts[1]] = max(best.get(parts[1], -1), n)
    out = []
    for k in fresh:
        v = links[k]
        if v.get("post") is None and v["org"] in best and v["openings"] <= best[v["org"]]:
            continue
        out.append(k)
    return out


def group_teletalk(items):
    """Several new posts of one organisation -> one Telegram message."""
    groups = {}
    for v in items:
        g = groups.setdefault(v["org"], {"url": v["url"], "org": v["org"], "text": v["org"],
                                         "dates": [], "apply": [], "site": None, "pdfs": [],
                                         "job": None})
        g["site"] = g["site"] or v.get("site")
        g["pdfs"] = g["pdfs"] or v.get("pdfs", [])
        g["job"] = g["job"] or v.get("job")
        if v.get("post"):
            g["apply"].append((v["post"], v.get("link"), v.get("vacancies")))
        else:
            g["text"] = v["text"]
    for g in groups.values():
        job = g["job"]
        if job:
            g["dates"] = [d for d in (job.get("start_date"), job.get("end_date")) if d]
            if job.get("details_url"):
                g["url"] = job["details_url"]
    return list(groups.values())


def org_site(org):
    """'Department of Agricultural Marketing (DAM)' -> http://dam.teletalk.com.bd/
    Only used if the site really opens."""
    codes = re.findall(r"\(\s*([A-Za-z0-9]+)\s*\)", org)
    if not codes:
        return None
    url = f"http://{codes[-1].lower()}.teletalk.com.bd/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code < 400 and ".teletalk.com.bd" in r.url:
            return r.url
    except requests.exceptions.RequestException:
        pass
    return None


NOT_CIRCULAR = [norm(w) for w in ["admit", "result", "seat plan", "প্রবেশপত্র", "ফলাফল"]]


def circular_pdfs(page_urls):
    """Look at the organisation's Teletalk site for the circular PDF (fallback only).
    Menus are kept here: Teletalk sites often put 'Job Circular' in the top menu."""
    found = []
    for u in [x for i, x in enumerate(page_urls) if x and x not in page_urls[:i]][:4]:
        if is_pdf_url(u):
            found.append(u)
            continue
        try:
            page_html, _, ctype, final = fetch_simple(u)
        except Exception as e:
            print(f"    could not open {u} for PDF: {str(e)[:100]}")
            continue
        if "pdf" in ctype.lower():
            found.append(final)
            continue
        soup = BeautifulSoup(page_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full = urllib.parse.urljoin(final, href)
            low = norm(clean(a.get_text(" ", strip=True)) + " " + full)
            if any(w in low for w in NOT_CIRCULAR):
                continue
            if is_pdf_url(full) or any(w in low for w in PDF_WORDS):
                found.append(full)
    out = []
    for u in found:
        if u not in out:
            out.append(u)
    return out[:3]


def unwrap_viewer(url):
    """Google/Office viewer links hide the real file in ?url=... ; return the real file."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("url", "file", "src"):
        if q.get(key) and q[key][0].startswith("http"):
            return q[key][0]
    return url


def parse_teletalk_job(page_html, page_url):
    """A job page on alljobs (/jobs/government/<org>?jobId=<id>): dates, views,
    advertisement number, apply link, circular, and every post of the organisation."""
    soup = BeautifulSoup(page_html, "html.parser")
    job = {"details_url": page_url}

    for box in soup.select("div.application-action"):
        label, value = box.find("h6"), box.find("span")
        if not label or not value:
            continue
        label, value = norm(clean(label.get_text(" "))), clean(value.get_text(" "))
        if "start" in label:
            job["start"] = value
        elif "end" in label or "deadline" in label:
            job["end"] = value
        elif "view" in label:
            job["viewed"] = value
    for key in ("start", "end"):
        d = find_dates(job.get(key, ""))
        job[key + "_date"] = d[0] if d else None

    m = re.search(r"ADV\s*NO\s*:?\s*([^\n]+)", soup.get_text("\n"), re.I)
    if m and clean(m.group(1)):
        job["adv"] = clean(m.group(1))
    badge = soup.select_one(".card-badge")
    if badge:
        job["status"] = clean(badge.get_text(" ")).replace("Status:", "").strip()

    for a in soup.find_all("a", href=True):
        t = norm(clean(a.get_text(" ")))
        full = urllib.parse.urljoin(page_url, a["href"])
        if "apply" in t and "alljobs.teletalk.com.bd/login" not in full:
            job["apply"] = full
            break

    frame = soup.select_one(".iframe-wrapper iframe[src], iframe[src*='advertisement']")
    if frame and frame["src"].startswith(("http", "/")):
        job["circular"] = unwrap_viewer(urllib.parse.urljoin(page_url, frame["src"]))

    # All posts of this organisation, each with its own page.
    base = urllib.parse.urlparse(page_url)
    job["posts"] = {}
    for card in soup.select("div.job-card-container"):
        name = card.find("h3")
        idm = re.search(r"GJOB(\d+)", card.get_text(" "))
        vac = re.search(r"(\d+)\s*\(vacanc", card.get_text(" ").translate(BN_DIGITS))
        if not name or not idm:
            continue
        link = urllib.parse.urlunparse(base._replace(query=f"jobId={idm.group(1)}", fragment=""))
        job["posts"][clean(name.get_text(" "))] = {
            "url": link, "vacancies": int(vac.group(1)) if vac else None}
    return job


def all_hrefs(page):
    try:
        return set(page.locator("a[href]").evaluate_all("els => els.map(e => e.href)"))
    except Exception:
        return set()


def open_post(context, list_url, org, post):
    """Open the list, pick this post in its organisation's 'See Openings' list and
    follow it. Returns (url, page_html) of where it went, or (None, None)."""
    page = context.new_page()
    try:
        page.goto(list_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("div.govt-job-card", timeout=45000)
        card = page.locator("div.govt-job-card").filter(
            has=page.locator("h4", has_text=org)).first
        for _ in range(30):
            if card.count():
                break
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(1200)
        if not card.count():
            return None, None
        card.locator("input[role='combobox']").first.click(timeout=5000)
        page.wait_for_timeout(1000)
        opts = page.locator("[id^='react-select-'][id*='-option-']")
        texts = [clean(t) for t in opts.all_inner_texts()]
        if post not in texts:
            return None, None
        tabs_before, url_before, links_before = len(context.pages), page.url, all_hrefs(page)
        opts.nth(texts.index(post)).click(timeout=5000)
        page.wait_for_timeout(3000)
        target = page
        if len(context.pages) > tabs_before:  # opened in a new tab
            target = context.pages[-1]
        elif page.url == url_before:  # nothing opened; maybe a new link appeared
            new_links = [h for h in all_hrefs(page) - links_before if h.startswith("http")]
            return (new_links[0] if new_links else None), None
        try:  # wait for the job page to fill in
            target.wait_for_selector("div.application-action", timeout=20000)
            target.wait_for_timeout(1500)
        except Exception:
            pass
        url, content = target.url, target.content()
        if target is not page:
            target.close()
        return (url if url and url != "about:blank" else None), content
    finally:
        page.close()


def add_teletalk_links(src, items):
    """For every post that will be sent: its job page, dates, views, apply link, circular."""
    items = list(items)[:MAX_CIRCULARS]
    if not items:
        return
    jobs = {}  # org -> parsed job page
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(user_agent=HEADERS["User-Agent"],
                                          ignore_https_errors=True,
                                          viewport={"width": 1366, "height": 900})
            for v in items:
                if not v.get("post"):
                    continue
                known = jobs.get(v["org"])
                if known and v["post"] in known["posts"]:
                    continue  # already listed on a job page of this organisation
                try:
                    url, content = open_post(context, src["url"], v["org"], v["post"])
                except Exception as e:
                    print(f"    could not open {v['text'][:50]}: {str(e)[:120]}")
                    continue
                if content and "application-action" in content:
                    job = parse_teletalk_job(content, url)
                    save_debug(f"job_{v['org'][:40]}", content)
                    if known:
                        known["posts"].update(job["posts"])
                    else:
                        jobs[v["org"]] = job
                    v["link"] = url
                elif url:
                    v["link"] = url
            browser.close()
    except Exception as e:
        print(f"    browser failed while opening job pages: {str(e)[:150]}")

    sites, pdfs = {}, {}
    for v in items:
        org, job = v["org"], jobs.get(v["org"])
        if job:
            v["job"] = job
            info = job["posts"].get(v.get("post") or "", {})
            v["link"] = v.get("link") or info.get("url")
            v["vacancies"] = info.get("vacancies")
            v["site"] = job.get("apply")
            v["pdfs"] = [job["circular"]] if job.get("circular") else []
        if not v.get("site") or not v.get("pdfs"):  # fallback: organisation's own site
            if org not in sites:
                sites[org] = org_site(org)
                pdfs[org] = circular_pdfs([sites[org]])
            v["site"] = v.get("site") or sites[org]
            v["pdfs"] = v.get("pdfs") or pdfs[org]
        print(f"    {v['text'][:50]} -> page: {v.get('link') or '-'}, apply: {v['site'] or '-'}, "
              f"circular: {(v['pdfs'] or ['-'])[0]}")


# ---------------------------------------------------------------- circular page


def parse_details(page_html, page_url):
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    pdfs, posts, used_urls, used_rows = [], [], {page_url.rstrip("/")}, set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urllib.parse.urljoin(page_url, href)
        if full.rstrip("/") in used_urls:
            continue
        text = clean(a.get_text(" ", strip=True))
        low = norm(text)
        if low in NAV_TEXTS:
            continue

        if is_pdf_url(full) or any(w in low for w in PDF_WORDS):
            pdfs.append((text[:60] or "Circular (PDF)", full))
            used_urls.add(full.rstrip("/"))
            continue

        row = a.find_parent("tr")
        if row is not None:
            if id(row) in used_rows:  # one entry per table row
                continue
            row_text = clean(row.get_text(" ", strip=True))
            if len(text) < 3 or low in GENERIC_TEXTS or len(row_text) < 120:
                text = row_text
        elif len(text) < 3 or low in GENERIC_TEXTS:
            continue
        elif not re.search(r"post|apply|job|position|vacan", full, re.I):
            continue  # loose link outside any table: probably not a post
        if len(text) < 3:
            continue
        text = tidy(text)
        posts.append((text[:100], full))
        used_urls.add(full.rstrip("/"))
        if row is not None:
            used_rows.add(id(row))

    # Some application sites list posts only in a drop-down, without own links.
    post_names = []
    for sel in soup.find_all("select"):
        label = norm((sel.get("name") or "") + " " + (sel.get("id") or ""))
        if "post" in label or "pos" in label:
            for opt in sel.find_all("option"):
                t = clean(opt.get_text(" ", strip=True))
                if t and "select" not in t.lower() and not opt.get("value", "x") in ("", "0"):
                    post_names.append(t[:100])

    # Lines like "Application Deadline: 30-11-2026"
    info = []
    for line in soup.get_text("\n", strip=True).split("\n"):
        line = clean(line)
        if (len(line) < 140 and find_dates(line)
                and any(w in norm(line) for w in INFO_WORDS) and line not in info):
            info.append(line)
        if len(info) >= 4:
            break

    return {"pdfs": pdfs[:3], "posts": posts[:MAX_POSTS], "post_names": post_names[:MAX_POSTS],
            "info": info, "total_posts": len(posts) or len(post_names)}


def get_details(url, label):
    if is_pdf_url(url):
        return {"pdfs": [("Circular (PDF)", url)], "posts": [], "post_names": [],
                "info": [], "total_posts": 0}
    page_html, status, ctype, _ = fetch_simple(url)
    if "html" not in ctype.lower() and "pdf" in ctype.lower():
        return {"pdfs": [("Circular (PDF)", url)], "posts": [], "post_names": [],
                "info": [], "total_posts": 0}
    d = parse_details(page_html, url)
    if status >= 400 or (not d["posts"] and not d["post_names"]):
        try:
            b_html, _ = fetch_browser(url, wait_ms=5000)  # maybe a JavaScript page
            b = parse_details(b_html, url)
            if b["total_posts"] >= d["total_posts"]:
                page_html, d = b_html, b
        except Exception as e:
            print(f"    browser failed on circular page: {str(e)[:150]}")
    save_debug("circular_" + label, page_html)
    print(f"    details: {d['total_posts']} posts, {len(d['pdfs'])} pdf, {len(d['info'])} date lines")
    return d

# ---------------------------------------------------------------- telegram


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[no Telegram keys set] would send:\n" + message + "\n")
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


def send_lines(lines):
    """Send lines, split into several messages if longer than Telegram's 4096 chars."""
    ok, chunk = True, []
    for line in lines:
        if chunk and len("\n".join(chunk + [line])) > 3800:
            ok = send_telegram("\n".join(chunk)) and ok
            chunk = ["(continued)"]
        chunk.append(line)
    if chunk:
        ok = send_telegram("\n".join(chunk)) and ok
    return ok


def nice_time(raw, day):
    """'2026-11-16 10:00 AM' -> '16 Nov 2026, 10:00 AM'"""
    if not day:
        return raw
    t = re.search(r"\d{1,2}:\d{2}\s*[AaPp][Mm]", raw or "")
    return f"{day:%d %b %Y}" + (f", {t.group(0).upper()}" if t else "")


def build_teletalk_message(source_name, item):
    e = html.escape
    tag = "🆕 New circular" if not MANUAL_RUN else "📢 Circular"
    lines = [f"🇧🇩 <b>{tag}</b>", "", f"📌 <b>{e(item['text'])}</b>", f"🏢 {e(source_name)}"]
    job = item.get("job") or {}
    if job.get("adv"):
        lines.append(f"🧾 ADV NO: {e(job['adv'])}")
    if job.get("start"):
        lines.append(f"🟢 Application start: {e(nice_time(job['start'], job.get('start_date')))}")
    if job.get("end"):
        line = f"🔴 Application end: {e(nice_time(job['end'], job.get('end_date')))}"
        if job.get("end_date"):
            left = (job["end_date"] - datetime.now(BD_TZ).date()).days
            if left >= 0:
                line += f" ({left} days left)" if left else " (today!)"
        lines.append(line)
    if job.get("viewed"):
        lines.append(f"👁 Viewed by: {e(job['viewed'])}")

    lines.append("")
    pdf_list = item.get("pdfs") or []
    for n, link in enumerate(pdf_list, 1):
        label = "Circular PDF" + (f" {n}" if len(pdf_list) > 1 else "")
        lines.append(f'📄 <a href="{e(link, quote=True)}">{label}</a>')
    if item.get("site"):
        lines.append(f'✅ <a href="{e(item["site"], quote=True)}">Apply online</a>')
    page_label = "Job page" if job else "Teletalk job list"
    lines.append(f'🔗 <a href="{e(item["url"], quote=True)}">{page_label}</a>')

    posts = item.get("apply") or []
    if posts:
        lines += ["", f"<b>Posts ({len(posts)}):</b>"]
        for i, (name, link, vac) in enumerate(posts[:MAX_POSTS], 1):
            line = f"{i}. {e(name)}"
            if vac:
                line += f" — {vac} {'vacancy' if vac == 1 else 'vacancies'}"
            if link:
                line += f' — <a href="{e(link, quote=True)}">Details</a>'
            lines.append(line)
        if len(posts) > MAX_POSTS:
            lines.append(f"…and {len(posts) - MAX_POSTS} more on the job page.")
    return lines


def build_message(source_name, item, details):
    e = html.escape
    tag = "🆕 New circular" if not MANUAL_RUN else "📢 Circular"
    lines = [f"🇧🇩 <b>{tag}</b>", "", f"📌 <b>{e(tidy(item['text']))}</b>", f"🏢 {e(source_name)}"]
    if item["dates"]:
        lines.append("📅 " + ", ".join(d.strftime("%d %b %Y") for d in item["dates"]))
    lines.append(f'🔗 <a href="{e(item["url"], quote=True)}">Open circular page</a>')

    if details:
        for line in details["info"]:
            lines.append(f"🗓 {e(line)}")
        for text, link in details["pdfs"]:
            lines.append(f'📄 <a href="{e(link, quote=True)}">{e(text)}</a>')
        if details["posts"]:
            lines += ["", f"<b>Posts ({details['total_posts']}):</b>"]
            for i, (text, link) in enumerate(details["posts"], 1):
                lines.append(f'{i}. <a href="{e(link, quote=True)}">{e(text)}</a>')
        elif details["post_names"]:
            lines += ["", f"<b>Posts ({details['total_posts']})</b> — apply from the circular page:"]
            for i, text in enumerate(details["post_names"], 1):
                lines.append(f"{i}. {e(text)}")
        else:
            lines += ["", "ℹ️ Post list not found automatically — open the circular page."]
        if details["total_posts"] > MAX_POSTS:
            lines.append(f"…and {details['total_posts'] - MAX_POSTS} more on the circular page.")
    return lines

# ---------------------------------------------------------------- main


def main():
    if os.environ.get("GITHUB_ACTIONS") and (not BOT_TOKEN or not CHAT_ID):
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret is missing or empty.\n"
              "Add them in Settings -> Secrets and variables -> Actions (exact names).")
        sys.exit(1)

    seen = load_seen()
    start, end = date_range()
    period = fmt_range(start, end)
    if MANUAL_RUN:
        print(f"Manual run: sending all circulars published {period} (plus anything new).")
        # Tell the user right away, so they know Telegram works even if a site fails later.
        if not send_telegram(f"🔎 Checking job sites for circulars published {period}…"):
            print("ERROR: Telegram message failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
            sys.exit(1)

    to_send = []  # (source, item)
    report = []   # one status line per site, sent to Telegram on manual runs
    for src in SOURCES:
        print(f"[{src['name']}] {src['url']}")
        try:
            links = read_teletalk(src) if src.get("type") == "teletalk" else extract_links(src)
        except Exception as e:
            print(f"[{src['name']}] failed: {e}")
            report.append(f"❌ {src['name']}: could not open site ({str(e)[:120]})")
            continue

        old_list = seen.get(src["name"], [])
        old = set(old_list)
        first_run = not old  # nothing saved yet for this site
        fresh = [k for k in links if k not in old]
        alert = fresh
        if src.get("type") == "teletalk":
            alert = teletalk_really_new(fresh, old, links)
        print(f"[{src['name']}] {len(links)} job links, {len(fresh)} new"
              + (" (first run: saving only, no alert)" if first_run and fresh else ""))

        chosen = {}
        if not first_run:
            for k in alert:
                chosen[k] = links[k]
        if MANUAL_RUN:
            todays = {k: v for k, v in links.items()
                      if published(v) and start <= published(v) <= end}
            with_dates = sum(1 for v in links.values() if v["dates"])
            print(f"[{src['name']}] {len(todays)} published {period} "
                  f"({with_dates} of {len(links)} links show a date)")
            if links and not with_dates:
                print(f"[{src['name']}] WARNING: no dates found on this page, "
                      "so circulars can't be picked by date. Send the debug artifact.")
            chosen.update(todays)
        status = f"{len(links)} links, {len(fresh)} new"
        if MANUAL_RUN:
            status += f", {len(todays)} in date range"
            if links and not with_dates:
                status += " ⚠️ no dates on page"
        if first_run and fresh:
            status += " (first run: saved only)"
        report.append(("⚠️ " if not links else "✅ ") + f"{src['name']}: {status}")
        if src.get("type") == "teletalk":
            add_teletalk_links(src, chosen.values())
            to_send += [(src, g) for g in group_teletalk(chosen.values())]
        else:
            to_send += [(src, v) for v in chosen.values()]

        # Keep the list from growing forever (oldest entries are dropped first).
        seen[src["name"]] = (old_list + fresh)[-2000:]

    if len(to_send) > MAX_CIRCULARS:
        print(f"WARNING: {len(to_send)} circulars; sending only the first {MAX_CIRCULARS}.")
        to_send = to_send[:MAX_CIRCULARS]

    all_sent = True
    for n, (src, item) in enumerate(to_send, 1):
        print(f"[{n}/{len(to_send)}] {item['text'][:70]}")
        if "apply" in item:  # Teletalk: everything is already collected
            all_sent = send_lines(build_teletalk_message(src["name"], item)) and all_sent
            continue
        details = None
        if src.get("details"):
            try:
                details = get_details(item["url"], f"{n}_{item['text'][:30]}")
            except Exception as e:
                print(f"    details failed: {e}")
        all_sent = send_lines(build_message(src["name"], item, details)) and all_sent

    all_sent = daily_summary(seen, datetime.now(BD_TZ).date()) and all_sent

    if MANUAL_RUN:
        # Manual run: always send a short report, so you can see the bot works.
        lines = [f"🤖 <b>Manual check finished</b> ({period})",
                 f"Circulars sent: {len(to_send)}", ""]
        lines += [html.escape(r) for r in report]
        all_sent = send_lines(lines) and all_sent

    if not all_sent:
        # Don't save, so these circulars are sent again on the next run.
        print("ERROR: Telegram message failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(1)

    save_seen(seen)
    print(f"Done. {len(to_send)} circular(s) sent.")


if __name__ == "__main__":
    main()
