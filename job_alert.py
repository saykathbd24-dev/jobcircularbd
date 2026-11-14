"""
BD Govt Job Alert -> Telegram
Checks Bangladesh government job circular websites and sends new
circulars to you through your own Telegram bot.

Scheduled run: sends only circulars that are new since the last run.
Manual run (MANUAL_RUN=true): also sends every circular published between
DATE_FROM and DATE_TO (Bangladesh time).
    DATE_FROM empty -> today          DATE_TO empty -> today
    so "From = 01-11-2026, To = empty" means 1 November until today.

How each site is read (written from the real pages, not guessed):
  Teletalk  - the site's own data feed (organisations, posts, publish date)
  Army      - "News & Updates" (date + title) and "CURRENT CIRCULARS"
  BPSC      - not read yet: the circular lists are on sub-pages. On manual runs
              those pages are saved in the debug folder so a reader can be written.
"""
import html
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# ---------------------------------------------------------------
# Websites to watch.
#   type "teletalk" -> alljobs.teletalk.com.bd (see read_teletalk)
#   type "army"     -> join.army.mil.bd        (see read_army)
#   type "collect"  -> only save the pages (manual runs), nothing is sent
# ---------------------------------------------------------------
SOURCES = [
    {"name": "Teletalk All Jobs", "type": "teletalk",
     "url": "https://alljobs.teletalk.com.bd/jobs/government"},
    {"name": "Bangladesh Army", "type": "army",
     "url": "https://joinbangladesharmy.army.mil.bd/"},
    {"name": "BPSC", "type": "collect", "pages": {
        "notices": "https://bpsc.gov.bd/pages/notices",
        "bcs_circulars": "https://bpsc.gov.bd/pages/psc-exams/6ab4f1d2a5bee3786104b263",
        "noncadre_circulars": "https://bpsc.gov.bd/pages/static-pages/6ad5e44f3c97e6c4ab675017",
        "bcs_exams": ("https://bpsc.gov.bd/pages/psc-exams?filters="
                      "%7B%22exam_type%22%3A%20%22691995ee933eb65569ddc1ba%22%7D"),
        "noncadre_exams": ("https://bpsc.gov.bd/pages/psc-exams?filters="
                           "%7B%22exam_type%22%3A%20%22691995ee933eb65569ddc1bb%22%7D"),
    }},
]

SEEN_FILE = "seen.json"
SEEN_VERSION = 4     # bump when the way circulars are remembered changes
DEBUG_DIR = "debug"  # downloaded pages are saved here
MAX_MESSAGES = 60    # safety limit per run
MAX_POSTS = 60       # posts listed in one message
DAILY_HOUR = 9       # daily Teletalk summary: first run after this hour (Bangladesh time)

TELETALK_HOME = "https://alljobs.teletalk.com.bd/"
TELETALK_API = "https://alljobs.teletalk.com.bd/api/v1/govt-jobs/org-list?page={page}&limit=20"
TELETALK_API_PATH = "/govt-jobs/org-list"
TELETALK_JOB = "https://alljobs.teletalk.com.bd/jobs/government/{org}?jobId={job}"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MANUAL_RUN = os.environ.get("MANUAL_RUN", "").strip().lower() == "true"
BD_TZ = timezone(timedelta(hours=6))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}


def clean(text):
    return " ".join((text or "").split())


def today_bd():
    return datetime.now(BD_TZ).date()

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


def find_dates(text):
    """All dates written in the text (English or Bangla digits), in order, no repeats."""
    text = (text or "").translate(BN_DIGITS)
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


def date_range():
    """Dates typed in the Run workflow form. Accepts 10-11-2026, 2026-11-10, 10 Nov 2026...
    From empty -> today.  To empty -> today.  (From = 01-11-2026 alone = 1 Nov until today.)"""
    today = today_bd()

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
    end = read("DATE_TO") or today
    if start > end:
        start, end = end, start
    return start, end


def fmt_range(start, end):
    return f"{start:%d %b %Y}" if start == end else f"{start:%d %b %Y} – {end:%d %b %Y}"


def utc_to_bd_date(stamp):
    """'2026-11-12T09:36:51.623Z' -> the date in Bangladesh (12 Nov 2026)."""
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BD_TZ).date()
    except (ValueError, TypeError):
        d = find_dates(str(stamp)[:10])
        return d[0] if d else None

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


def save_debug(name, content, ext="html"):
    """Keep what the bot downloaded, so problems can be fixed from the real pages."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]
        with open(os.path.join(DEBUG_DIR, f"{safe}.{ext}"), "w", encoding="utf-8") as f:
            if ext == "json":
                json.dump(content, f, ensure_ascii=False, indent=1)
            else:
                f.write(content)
    except (OSError, TypeError, ValueError) as e:
        print(f"    (could not save debug file {name}: {e})")

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


def get_json(url, referer=None):
    """Download a JSON address. Returns the data, or raises RuntimeError."""
    headers = dict(HEADERS, Accept="application/json, text/plain, */*")
    if referer:
        headers["Referer"] = referer
    last = "no answer"
    for verify in (True, False):
        try:
            r = requests.get(url, headers=headers, timeout=30, verify=verify)
        except requests.exceptions.SSLError as e:
            last = f"SSL {str(e)[:80]}"
            continue  # try again without certificate check
        except requests.exceptions.RequestException as e:
            raise RuntimeError(type(e).__name__)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError("answer is not JSON")
    raise RuntimeError(last)


def keep_json(store, response):
    """Remember JSON answers a page receives from its own site (not ads/analytics)."""
    try:
        if len(store) >= 25 or "json" not in response.headers.get("content-type", ""):
            return
        host = urllib.parse.urlparse(response.url).netloc
        if any(w in host for w in ("google", "doubleclick", "facebook", "plausible")):
            return
        store.append({"url": response.url, "data": response.json()})
    except Exception:
        pass


def fetch_browser(url, wait_ms=8000, json_store=None):
    """Open the page in a real (headless) Chrome so JavaScript-built pages load."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], ignore_https_errors=True)
        if json_store is not None:
            page.on("response", lambda r: keep_json(json_store, r))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(wait_ms)  # give the page time to load its data
        except Exception as e:
            print(f"    browser warning: {str(e)[:200]}")
        content, final_url = page.content(), page.url
        browser.close()
    return content, final_url

# ---------------------------------------------------------------- alljobs.teletalk
# The site loads its list from TELETALK_API. One organisation looks like:
#   {"id": 656, "name": "Department of Agricultural Marketing (DAM)",
#    "website": "http://www.dam.gov.bd/", "job_created_at": "2026-11-12T09:36:51.623Z",
#    "govt_jobs": [{"id": 15307, "job_title": "...", "job_title_bn": "..."}, ...]}
# and the answer also has "count" = number of organisations in total (20 per page).


def valid_org_page(data):
    return isinstance(data, dict) and isinstance(data.get("govtOrgJobs"), list)


def page_count(first):
    per_page = len(first["govtOrgJobs"]) or 20
    total = first.get("count") or per_page
    return min(max(1, math.ceil(total / per_page)), 30)


def teletalk_by_requests(list_url):
    """Read the data feed directly. Returns {page number: data}."""
    pages = {1: get_json(TELETALK_API.format(page=1), list_url)}
    if not valid_org_page(pages[1]):
        raise RuntimeError("unexpected answer from the data feed")
    for n in range(2, page_count(pages[1]) + 1):
        data = get_json(TELETALK_API.format(page=n), list_url)
        if not valid_org_page(data):
            raise RuntimeError(f"unexpected answer for page {n}")
        if not data["govtOrgJobs"]:
            break
        pages[n] = data
    return pages


def teletalk_by_browser(list_url):
    """Fallback: open the list in Chrome and take the data the site loads for itself.
    Further pages: ask for them the same way the site did; if that is refused,
    click the 'next' button like a visitor. Returns {page number: data}."""
    from playwright.sync_api import sync_playwright
    pages, sent_headers = {}, {}

    def on_response(r):
        if TELETALK_API_PATH not in r.url:
            return
        try:
            data = r.json()
            if valid_org_page(data):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(r.url).query)
                pages[int(q.get("page", ["1"])[0])] = data
                sent_headers.update(r.request.headers)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(user_agent=HEADERS["User-Agent"], ignore_https_errors=True,
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("response", on_response)

        def wait_for(n, seconds):
            for _ in range(seconds * 2):
                if n in pages:
                    return True
                page.wait_for_timeout(500)
            return n in pages

        try:
            page.goto(list_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"    browser warning: {str(e)[:200]}")
        if not wait_for(1, 45):
            save_debug("teletalk_list_page", page.content())
            browser.close()
            raise RuntimeError("the list page opened but its data never arrived")

        skip = ("host", "content-length", "cookie", "accept-encoding", "connection")
        replay_headers = {k: v for k, v in sent_headers.items()
                          if not k.startswith(":") and k.lower() not in skip}
        can_replay, shown = True, 1  # shown = page number the browser is displaying
        for n in range(2, page_count(pages[1]) + 1):
            if can_replay:
                try:
                    r = context.request.get(TELETALK_API.format(page=n),
                                            headers=replay_headers, timeout=40000)
                    data = r.json() if r.ok else None
                    if valid_org_page(data):
                        pages[n] = data
                except Exception:
                    pass
                can_replay = n in pages
            try:
                while n not in pages and shown < n:
                    page.locator("ul.pagination li.next a").first.click(timeout=8000)
                    shown += 1
                    wait_for(shown, 25)
            except Exception as e:
                print(f"    could not click to page {n}: {str(e)[:120]}")
            if n not in pages:
                print(f"    WARNING: page {n} of the list could not be read")
                break
        save_debug("teletalk_list_page", page.content())
        browser.close()
    return pages


def teletalk_items(pages):
    """Turn the data feed into items: one per post, grouped by organisation."""
    items, orgs_seen = {}, set()
    for n in sorted(pages):
        for org in pages[n]["govtOrgJobs"]:
            org_id, name = org.get("id"), clean(org.get("name") or org.get("short_name"))
            if org_id is None or not name or org_id in orgs_seen:
                continue
            orgs_seen.add(org_id)
            jobs = [j for j in (org.get("govt_jobs") or []) if j.get("id") is not None]
            first = jobs[0]["id"] if jobs else ""
            links = []
            if (org.get("website") or "").startswith("http"):
                links.append(("🌐", "Organisation website", org["website"].strip()))
            base = {
                "title": name,
                "url": TELETALK_JOB.format(org=org_id, job=first),
                "url_label": "Job page (dates, circular, apply)",
                "published": utc_to_bd_date(org.get("job_created_at")),
                "info": [], "links": links,
                "group": f"teletalk-org-{org_id}", "total_posts": len(jobs),
            }
            if not jobs:
                items[f"teletalk | {org_id} | -"] = dict(base, post=None)
            for j in jobs:
                title = clean(j.get("job_title_bn")) or clean(j.get("job_title")) or f"Post {j['id']}"
                link = TELETALK_JOB.format(org=org_id, job=j["id"])
                items[f"teletalk | {org_id} | {j['id']}"] = dict(base, post=(title, link))
    return items, len(orgs_seen)


def read_teletalk(src):
    pages, how = None, "data feed"
    try:
        pages = teletalk_by_requests(src["url"])
    except Exception as e:
        print(f"    direct read of the data feed failed ({e}); opening the site in Chrome instead")
    if pages is None:
        pages, how = teletalk_by_browser(src["url"]), "data feed via Chrome"
    save_debug("teletalk_api", [{"page": n, "data": pages[n]} for n in sorted(pages)], "json")

    items, orgs = teletalk_items(pages)
    expected = pages[1].get("count") or orgs
    note = f"{orgs} organisations, {len(items)} posts"
    if orgs < expected:
        note += f" (⚠️ only {orgs} of {expected} organisations could be read)"
    print(f"    {how}: {note}")
    if not items:
        raise RuntimeError("the data feed answered but had no organisations in it")
    return items, note


def save_teletalk_sample(items):
    """Manual runs: keep one job page (and the data it loads), so that deadline,
    circular PDF and apply link can be added to the messages later."""
    first = next((v for v in items.values() if v.get("post")), None)
    if not first:
        return
    try:
        store = []
        page_html, _ = fetch_browser(first["post"][1], wait_ms=8000, json_store=store)
        save_debug("teletalk_job_page", page_html)
        save_debug("teletalk_job_page_api", store, "json")
    except Exception as e:
        print(f"    (could not save a sample job page: {str(e)[:120]})")


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

# ---------------------------------------------------------------- Bangladesh Army
# Home page of join.army.mil.bd:
#   News & Updates:     <div class="user-activity__item"> <span>12/11/2026</span>
#                       <a href=".../news-view/XXXX">title</a> <span class="badge">@notice</span>
#   CURRENT CIRCULARS:  one <div class="card"> per course: <h4>98TH BMA LONG COURSE</h4>,
#                       "Publish Date: 29 July 2026", "Download Circular" link,
#                       a table row "Starts: 13 November 2026 ... Ends: 21 November 2026 ..."


def days_left(day):
    left = (day - today_bd()).days
    if left < 0:
        return " (closed)"
    return " (today!)" if left == 0 else f" ({left} days left)"


def parse_army(page_html, page_url):
    soup = BeautifulSoup(page_html, "html.parser")
    items = {}

    head = soup.find(lambda t: t.name in ("h2", "h3", "h4", "h5", "h6")
                     and "current circular" in t.get_text(" ", strip=True).lower())
    card = head.find_parent("div", class_="card") if head else None
    body = card.find("div", class_="card-body", recursive=False) if card else None
    for block in (body.find_all("div", class_="card", recursive=False) if body else []):
        name = block.find(["h4", "h3", "h5"])
        title = clean(name.get_text(" ", strip=True)) if name else ""
        if not title:
            continue
        text = clean(block.get_text(" ", strip=True))
        m = re.search(r"Publish(?:ed)?\s*Date\s*:?\s*(.{0,40})", text, re.I)
        pub = find_dates(m.group(1)) if m else []
        info = []
        m = re.search(r"Starts?\s*:\s*(.{0,45}?)\s*Ends?\s*:\s*(.{0,45})", text, re.I)
        if m:
            for label, icon, part in (("start", "🟢", m.group(1)), ("end", "🔴", m.group(2))):
                d = find_dates(part)
                if d:
                    info.append(f"{icon} Application {label}: {d[0]:%d %b %Y}"
                                + (days_left(d[0]) if label == "end" else ""))
        links = []
        for a in block.find_all("a", href=True):
            if "circular" in a.get_text(" ", strip=True).lower() or a.has_attr("download"):
                links.append(("📄", "Circular", urllib.parse.urljoin(page_url, a["href"])))
                break
        apply_url = page_url.split("#")[0] + ("#" + name["id"] if name.get("id") else "")
        key = f"army-circular | {title} | {pub[0].isoformat() if pub else ''}"
        items[key] = {"title": title + " — circular", "url": apply_url,
                      "url_label": "Apply / details", "published": pub[0] if pub else None,
                      "info": info, "links": links, "group": None, "post": None}

    for row in soup.select("div.user-activity__item"):
        a = row.find("a", href=True)
        title = clean(a.get_text(" ", strip=True)) if a else ""
        if not title:
            continue
        stamp = row.find("span")
        dates = find_dates(stamp.get_text(" ", strip=True)) if stamp else []
        if not dates:
            dates = find_dates(row.get_text(" ", strip=True))
        badge = row.find(class_="badge")
        tag = clean(badge.get_text(" ", strip=True)).lstrip("@") if badge else ""
        url = urllib.parse.urljoin(page_url, a["href"].strip())
        items[f"army-news | {url}"] = {
            "title": title, "url": url, "url_label": "Open notice",
            "published": dates[0] if dates else None,
            "info": [f"🏷 {tag}"] if tag else [], "links": [], "group": None, "post": None}
    return items


def read_army(src):
    page_html, status, _, final_url = fetch_simple(src["url"])
    items = parse_army(page_html, final_url)
    if not items:  # maybe the page needs JavaScript
        try:
            page_html, final_url = fetch_browser(src["url"])
            items = parse_army(page_html, final_url)
        except Exception as e:
            print(f"    browser fetch failed: {str(e)[:150]}")
    save_debug("list_Bangladesh_Army", page_html)
    if not items:
        raise RuntimeError(f"HTTP {status}: news list and circulars not found (page layout changed?)")
    news = sum(1 for k in items if k.startswith("army-news"))
    note = f"{news} news, {len(items) - news} current circular(s)"
    print(f"    {note}")
    return items, note

# ---------------------------------------------------------------- collect only


def collect_pages(src):
    """Save pages exactly as the site gives them (plain download, Chrome, and the
    data Chrome receives). Nothing is sent from these pages yet."""
    saved = 0
    for label, url in src["pages"].items():
        name = f"{src['name']}_{label}"
        try:
            page_html, status, _, _ = fetch_simple(url)
            save_debug(name + "_plain", page_html)
            print(f"    {label}: plain download HTTP {status}, {len(page_html)} bytes")
            saved += 1
        except Exception as e:
            print(f"    {label}: plain download failed: {str(e)[:120]}")
        try:
            store = []
            page_html, _ = fetch_browser(url, wait_ms=7000, json_store=store)
            save_debug(name + "_chrome", page_html)
            save_debug(name + "_api", store, "json")
            print(f"    {label}: Chrome {len(page_html)} bytes, {len(store)} data answers")
        except Exception as e:
            print(f"    {label}: Chrome failed: {str(e)[:120]}")
    return saved

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
    for attempt in range(4):
        r = requests.post(url, data=data, timeout=40)
        if r.status_code != 429:
            break
        try:  # Telegram says how long to wait when messages come too fast
            wait = int(r.json().get("parameters", {}).get("retry_after", 5))
        except (ValueError, AttributeError):
            wait = 5
        print(f"Telegram: too fast, waiting {wait}s")
        time.sleep(min(wait, 60) + 1)
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


def group_items(items):
    """Several posts of one organisation -> one message."""
    out, groups = [], {}
    for it in items:
        g = it.get("group")
        if g is None:
            out.append(dict(it, posts=[]))
            continue
        if g not in groups:
            groups[g] = dict(it, posts=[])
            out.append(groups[g])
        if it.get("post"):
            groups[g]["posts"].append(it["post"])
    return out


def build_message(source_name, item):
    e = html.escape
    tag = "📢 Circular" if MANUAL_RUN else "🆕 New circular"
    lines = [f"🇧🇩 <b>{tag}</b>", "", f"📌 <b>{e(item['title'])}</b>", f"🏢 {e(source_name)}"]
    if item.get("published"):
        lines.append(f"📅 Published: {item['published']:%d %b %Y}")
    lines += [e(t) for t in item.get("info", [])]
    lines.append("")
    lines.append(f'🔗 <a href="{e(item["url"], quote=True)}">{e(item.get("url_label") or "Open")}</a>')
    for icon, label, link in item.get("links", []):
        lines.append(f'{icon} <a href="{e(link, quote=True)}">{e(label)}</a>')

    posts = item.get("posts") or []
    if posts:
        total = item.get("total_posts") or len(posts)
        head = f"Posts ({len(posts)})" if len(posts) >= total else f"New posts ({len(posts)} of {total})"
        lines += ["", f"<b>{head}:</b>"]
        for i, (name, link) in enumerate(posts[:MAX_POSTS], 1):
            lines.append(f'{i}. <a href="{e(link, quote=True)}">{e(name)}</a>')
        if len(posts) > MAX_POSTS:
            lines.append(f"…and {len(posts) - MAX_POSTS} more on the job page.")
    return lines

# ---------------------------------------------------------------- main

READERS = {"teletalk": read_teletalk, "army": read_army}


def main():
    if os.environ.get("GITHUB_ACTIONS") and (not BOT_TOKEN or not CHAT_ID):
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret is missing or empty.\n"
              "Add them in Settings -> Secrets and variables -> Actions (exact names).")
        sys.exit(1)

    shutil.rmtree(DEBUG_DIR, ignore_errors=True)  # only this run's pages
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
        name = src["name"]
        if src["type"] == "collect":
            if MANUAL_RUN:
                print(f"[{name}] saving pages (not read yet)")
                n = collect_pages(src)
                report.append(f"🛠 {name}: not read yet — {n} of {len(src['pages'])} "
                              "pages saved so a reader can be written")
            continue

        print(f"[{name}] {src['url']}")
        try:
            items, note = READERS[src["type"]](src)
        except Exception as e:
            print(f"[{name}] failed: {e}")
            report.append(f"❌ {name}: could not read site ({str(e)[:150]})")
            continue

        old_list = seen.get(name, [])
        old = set(old_list)
        first_run = not old  # nothing saved yet for this site
        fresh = [k for k in items if k not in old]
        wanted = set() if first_run else set(fresh)
        in_range = set()
        if MANUAL_RUN:
            in_range = {k for k, v in items.items()
                        if v["published"] and start <= v["published"] <= end}
            wanted |= in_range
        chosen = group_items([items[k] for k in items if k in wanted])
        to_send += [(src, g) for g in chosen]

        new_n = len(group_items([items[k] for k in fresh]))
        if MANUAL_RUN:
            n_range = len(group_items([items[k] for k in items if k in in_range]))
            status = f"{note} — {n_range} published {period}"
            if len(chosen) > n_range:
                status += f", plus {len(chosen) - n_range} new since the last run"
            undated = sum(1 for v in items.values() if not v["published"])
            if undated:
                status += f"; ⚠️ {undated} without a date"
        else:
            status = f"{note}; {new_n} new"
            if first_run and fresh:
                status += " (first run: saved, not sent)"
        print(f"[{name}] {status}")
        report.append(f"✅ {name}: {status}")

        if MANUAL_RUN and src["type"] == "teletalk":
            save_teletalk_sample(items)
        # Keep the list from growing forever (oldest entries are dropped first).
        seen[name] = (old_list + fresh)[-5000:]

    found = len(to_send)
    if found > MAX_MESSAGES:
        print(f"WARNING: {found} circulars; sending only the first {MAX_MESSAGES}.")
        to_send = to_send[:MAX_MESSAGES]

    all_sent = True
    for n, (src, item) in enumerate(to_send, 1):
        print(f"[{n}/{len(to_send)}] {item['title'][:70]}")
        all_sent = send_lines(build_message(src["name"], item)) and all_sent

    all_sent = daily_summary(seen, today_bd()) and all_sent

    if MANUAL_RUN:
        # Manual run: always send a short report, so you can see the bot works.
        sent = f"Circulars sent: {len(to_send)}"
        if found > len(to_send):
            sent += f" of {found} found (limit {MAX_MESSAGES} per run — use a shorter date range)"
        lines = [f"🤖 <b>Manual check finished</b> ({period})", sent, ""]
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
