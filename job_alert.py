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
  Teletalk  - the site's own data feed (organisations, posts, publish date);
              then each post's job page for the circular PDF and the application form
  Army      - "News & Updates" (date + title) and "CURRENT CIRCULARS"
  BPSC      - not read yet: the circular lists are on sub-pages. On manual runs
              those pages are saved in the debug folder so a reader can be written.
"""
import difflib
import html
import json
import math
import os
import re
import shutil
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
                "short_name": clean(org.get("short_name")),
            }
            if not jobs:
                items[f"teletalk | {org_id} | -"] = dict(base, post=None)
            for j in jobs:
                title = clean(j.get("job_title_bn")) or clean(j.get("job_title")) or f"Post {j['id']}"
                post = {"title": title, "url": TELETALK_JOB.format(org=org_id, job=j["id"]),
                        "org": org_id, "job": j["id"], "title_en": clean(j.get("job_title")),
                        "title_bn": clean(j.get("job_title_bn"))}
                items[f"teletalk | {org_id} | {j['id']}"] = dict(base, post=post)
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


# ---------------------------------------------------------------- Teletalk job pages
# Every post has its own job page on alljobs (TELETALK_JOB). That page shows the
# circular PDF and the button to the application form. The bot opens the first
# job page in Chrome, takes both links from it, and notes which data answer of the
# page held them. The other posts are then read from that same data answer directly
# (fast); a post that can't be read that way is opened in Chrome too.
# Only used when the organisation's apply site can't be found from its short name
# (see "Teletalk apply sites" below): the job page links to the apply site.

LINK_CACHE = "_teletalk_links"   # in seen.json: job id -> links found earlier
MAX_JOB_PAGES = 120              # job pages opened in Chrome per run (safety limit)
TELETALK_SELF = {"alljobs.teletalk.com.bd", "teletalk.com.bd", "www.teletalk.com.bd"}
CIRCULAR_WORDS = ("circular", "বিজ্ঞপ্তি", "pdf")
APPLY_WORDS = ("apply", "আবেদন")
LINK_KINDS = ("pdf", "apply")
JOB_MARK = "@@JOB@@"
JOB_PAGE_READY_JS = r"""() => [...document.querySelectorAll('a[href], iframe[src], embed[src]')]
  .some(e => /\.pdf([?#]|$)/i.test(e.href || e.src || '')
          || /apply|আবেদন/i.test(e.textContent || ''))"""


def tidy_url(url):
    """Make a link safe for Telegram (spaces, Bangla letters -> %xx)."""
    return urllib.parse.quote(url.strip(), safe=":/?&=%#+@,;~!$'()*[]")


def host_of(url):
    return urllib.parse.urlparse(url).netloc.lower().split(":")[0]


def is_pdf(url):
    return urllib.parse.unquote(urllib.parse.urlparse(url).path).lower().endswith(".pdf")


def is_apply_site(url):
    """Teletalk apply sites are sub-sites like http://ccb.teletalk.com.bd/"""
    h = host_of(url)
    return h.endswith(".teletalk.com.bd") and h not in TELETALK_SELF


def has_job_id(text, job):
    return re.search(rf"(?<!\d){job}(?!\d)", str(text)) is not None


def walk(data, path=()):
    """Every single value inside JSON data, with the keys that lead to it."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from walk(v, path + (k,))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from walk(v, path + (i,))
    else:
        yield path, data


def at_path(data, path):
    for p in path:
        try:
            data = data[p]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def next_data(page_html):
    """Data some sites write into the page itself (<script id="__NEXT_DATA__">)."""
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html or "", re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def find_job_links(page_html, page_url):
    """Circular PDF and application form link on one job page (menus/footer left out)."""
    soup = BeautifulSoup(page_html, "html.parser")
    links = []
    for tag in soup.find_all(["a", "iframe", "embed", "object"]):
        if tag.find_parent(["nav", "header", "footer"]):
            continue
        raw = tag.get("data") if tag.name == "object" else (tag.get("href") or tag.get("src"))
        raw = (raw or "").strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urllib.parse.urljoin(page_url, raw)
        if url.startswith("http"):
            text = clean(tag.get_text(" ", strip=True) + " " + (tag.get("title") or "")).lower()
            links.append((text, url))

    pdf = (next((u for _, u in links if is_pdf(u)), None)
           or next((u for t, u in links if any(w in t for w in CIRCULAR_WORDS)
                    and host_of(u) not in TELETALK_SELF), None))
    apply, best = None, 0
    here = page_url.split("#")[0]
    for t, u in links:
        if is_pdf(u) or u.split("#")[0] == here:
            continue
        score = 2 * any(w in t for w in APPLY_WORDS) + is_apply_site(u)
        if score > best:
            best, apply = score, u
    return {"pdf": pdf, "apply": apply}


def links_in_data(answers):
    """Links written straight into the page's data (used when the page showed none)."""
    found = {"pdf": None, "apply": None}
    for ans in answers:
        for _, v in walk(ans["data"]):
            if not isinstance(v, str) or not v.startswith("http"):
                continue
            if not found["pdf"] and is_pdf(v):
                found["pdf"] = v
            elif not found["apply"] and is_apply_site(v):
                found["apply"] = v
    return found


def learn_recipe(answers, job, found):
    """Which data answer of the job page held its links, and where in it.
    With that, the other posts can be read without opening Chrome."""
    for ans in answers:
        if ans["method"] != "GET" or not has_job_id(ans["url"], job):
            continue
        spots = {}
        for kind in LINK_KINDS:
            target = urllib.parse.unquote(found.get(kind) or "")
            if not target:
                continue
            best = None
            for path, v in walk(ans["data"]):
                v = urllib.parse.unquote(v.strip()) if isinstance(v, str) else ""
                if len(v) >= 8 and target.endswith(v) and (not best or len(v) > best[2]):
                    best = (path, target[:-len(v)], len(v))
            if best:
                spots[kind] = best[:2]
        if spots and all(not found.get(k) or k in spots for k in LINK_KINDS):
            skip = ("host", "content-length", "cookie", "accept-encoding", "connection")
            headers = {k: v for k, v in (ans.get("headers") or {}).items()
                       if not k.startswith(":") and k.lower() not in skip}
            return {"template": re.sub(rf"(?<!\d){job}(?!\d)", JOB_MARK, ans["url"]),
                    "kind": ans["kind"], "headers": headers, "spots": spots}
    return None


class JobPageReader:
    """Finds the circular PDF and the application form of Teletalk posts."""

    def __init__(self, cache):
        self.cache = cache
        self.recipe = None
        self.visits = self.hits = 0
        self.pw = self.browser = self.context = self.page = None
        self.broken = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass

    def _start(self):
        if self.page:
            return
        if self.broken:
            raise RuntimeError("Chrome could not start")
        try:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch()
            self.context = self.browser.new_context(user_agent=HEADERS["User-Agent"],
                                                    ignore_https_errors=True)
            # pictures and fonts are not needed -> pages load faster
            self.context.route("**/*", lambda route: route.abort()
                               if route.request.resource_type in ("image", "font", "media")
                               else route.continue_())
            self.page = self.context.new_page()
        except Exception:
            self.broken = True
            raise

    def links_for(self, org, job):
        key = str(job)
        if key in self.cache:
            return self.cache[key]
        found = None
        if self.recipe:
            try:
                found = self._read_with_recipe(job)
            except Exception as e:
                print(f"    job {job}: quick read failed ({str(e)[:80]}), opening the page")
        if found is None:
            if self.visits >= MAX_JOB_PAGES or (self.visits >= 3 and not self.hits):
                return {}  # limit reached, or the job pages don't show these links
            found = self._visit(org, job)
        if found.get("pdf") or found.get("apply"):
            self.hits += 1
        if found.get("apply"):  # forms sometimes open later, so only keep complete finds
            self.cache[key] = found
        return found

    def _read_with_recipe(self, job):
        r = self.recipe
        url = r["template"].replace(JOB_MARK, str(job))
        if r["kind"] == "json":
            resp = self.context.request.get(url, headers=r["headers"], timeout=30000)
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}")
            data = resp.json()
        else:
            data = next_data(requests.get(url, headers=HEADERS, timeout=30, verify=False).text)
        if data is None:
            raise RuntimeError("no data in the answer")
        out = {}
        for kind in LINK_KINDS:
            spot = r["spots"].get(kind)
            v = at_path(data, spot[0]) if spot else None
            v = v.strip() if isinstance(v, str) else ""
            out[kind] = tidy_url(v if v.startswith("http") else spot[1] + v) if v else None
        if not any(out.values()):
            raise RuntimeError("links not in the answer")
        return out

    def _visit(self, org, job):
        self._start()
        self.visits += 1
        answers = []

        def on_response(resp):
            try:
                if TELETALK_API_PATH in resp.url or "json" not in resp.headers.get("content-type", ""):
                    return
                answers.append({"url": resp.url, "method": resp.request.method, "kind": "json",
                                "headers": resp.request.headers, "data": resp.json()})
            except Exception:
                pass

        url = TELETALK_JOB.format(org=org, job=job)
        self.page.on("response", on_response)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                self.page.wait_for_function(JOB_PAGE_READY_JS, timeout=15000)
            except Exception:
                pass  # links never showed up; whatever is there is checked below
            self.page.wait_for_timeout(1500)
            page_html, final_url = self.page.content(), self.page.url
        finally:
            self.page.remove_listener("response", on_response)

        nd = next_data(page_html)
        if nd is not None:
            answers.append({"url": url, "method": "GET", "kind": "next", "data": nd})
        if self.visits == 1:  # keep one real job page, to check the links by hand
            save_debug("teletalk_job_page", page_html)
            save_debug("teletalk_job_page_api", [{"url": a["url"], "data": a["data"]}
                                                 for a in answers], "json")

        found = find_job_links(page_html, final_url)
        mine = [a for a in answers if has_job_id(a["url"], job)]
        from_data = links_in_data(mine)
        for kind in LINK_KINDS:
            found[kind] = tidy_url(found[kind] or from_data[kind] or "") or None
        if not self.recipe and (found["pdf"] or found["apply"]):
            self.recipe = learn_recipe(answers, job, found)
            if self.recipe:
                print("    job pages: learned where the links are; other posts are read directly")
        print(f"    job {job}: PDF {'✓' if found['pdf'] else '✗'}, form {'✓' if found['apply'] else '✗'}")
        return found


# ---------------------------------------------------------------- Teletalk apply sites
# Every organisation has its own apply site, named after its short name in the data
# feed (BPC -> https://bpc.teletalk.com.bd/). Written from the real pages:
#  1. home page:   <fieldset><legend>Ongoing Circular</legend>
#                    <a href=".../bpc2026_officer/">Circular Ref: ..., Dated: 19 November 2026</a>
#                  (a "Previous Circular" fieldset follows; those are left out)
#  2. folder page  .../bpc2026_officer/:
#                    <a href=".../docs/20261119.pdf">Advertisement [Deadline: 19 Dec 2026 04:59:59 PM]</a>
#                    <a href=".../circulars.php">Application Form (Click here to Apply Online)</a>
#  3. circulars.php: <form action=".../posts.php" method="get">, one radio per circular
#                    <input name="circular_id" value="xpoPbGzIP2o="> + its PDF, button "submitCircular"
#  4. posts.php?circular_id=...&submitCircular=   (= the list of all posts)
#                    <form action=".../premium.php" method="get">, one radio per post
#                    <label><input name="post_code" value="xZsN"> English name
#                           <span>[বাংলা নাম]</span></label>, button "submitPost"
#  5. premium.php?post_code=xZsN&submitPost=      ("Alljobs Premium Member? Yes / No")
#                    <form action=".../application.php" method="get">
#                      <input type="hidden" name="post_code" value="xZkN">
#                      <input type="hidden" name="info" value="xc5fPmuf...">   (made by the site)
#                      radio nid: 1 = Yes, 2 = No;  text alljobs_id;  button "submitPremium"
#  6. application.php?post_code=...&info=...&alljobs_id=&nid=2&submitPremium=
#                    (= "No" chosen -> the application form itself; this is the post's link)
# All plain pages, so no Chrome is needed.

def apply_site_root(url):
    u = urllib.parse.urlparse(url)
    return f"{u.scheme}://{u.netloc}/"


def circular_folder(url):
    """https://bpc.teletalk.com.bd/bpc2026_officer/anything -> .../bpc2026_officer/
    (a folder's name starts with the sub-site's name, so other paths are not taken)."""
    u = urllib.parse.urlparse(url)
    code = host_of(url).split(".")[0]
    first = next((p for p in u.path.split("/") if p), "")
    if code and first.lower().startswith(code) and "." not in first:
        return f"{u.scheme}://{u.netloc}/{first}/"
    return None


def site_get(session, url):
    r = session.get(url, headers=HEADERS, timeout=25, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    return BeautifulSoup(r.text, "html.parser"), r.url


def radio_form(soup, base, radio_name):
    """A GET form with radio buttons -> [(label text, link that submits it, pdf near it)]."""
    first = soup.find("input", attrs={"type": "radio", "name": radio_name})
    form = first.find_parent("form") if first else None
    if not form or (form.get("method") or "get").lower() != "get":
        return []
    action = urllib.parse.urljoin(base, form.get("action") or base)
    button = form.find(["button", "input"], attrs={"type": "submit"})
    extra = [(button["name"], "")] if button and button.get("name") else []
    out = []
    for radio in form.find_all("input", attrs={"type": "radio", "name": radio_name}):
        label = radio.find_parent("label") or radio.parent
        holder = radio.find_parent("li") or label
        pdf = next((urllib.parse.urljoin(base, a["href"]) for a in holder.find_all("a", href=True)
                    if is_pdf(urllib.parse.urljoin(base, a["href"]))), None)
        query = urllib.parse.urlencode([(radio_name, radio.get("value", ""))] + extra)
        out.append((clean(label.get_text(" ", strip=True)), f"{action}?{query}", pdf))
    return out


def choose_no(premium_url):
    """Open a post's premium.php, choose "No" and give the link the Next button would
    open (application.php ...), with the fields in the same order the browser sends them.
    None = page not as expected (the post then keeps its premium.php link)."""
    try:
        page, base = site_get(requests.Session(), premium_url)
    except (requests.exceptions.RequestException, RuntimeError):
        return None
    no = None
    for radio in page.find_all("input", attrs={"type": "radio"}):
        label = radio.find_parent("label")
        if label and clean(label.get_text(" ", strip=True)).lower() in ("no", "না"):
            no = radio
            break
    form = no.find_parent("form") if no else None
    if not form or (form.get("method") or "get").lower() != "get" or not form.get("action"):
        return None
    fields = []
    for tag in form.find_all(["input", "select", "textarea", "button"]):
        name, kind = tag.get("name"), (tag.get("type") or "").lower()
        if not name or tag.has_attr("disabled") and tag.name != "button":
            continue
        if tag.name == "button" or kind == "submit":
            fields.append((name, tag.get("value", "")))
            break  # the Next button ends the form
        if kind in ("radio", "checkbox"):
            if tag is no:
                fields.append((name, tag.get("value", "")))
        elif tag.name == "select":
            opt = tag.find("option", selected=True) or tag.find("option")
            fields.append((name, opt.get("value", "") if opt else ""))
        else:
            fields.append((name, tag.get("value", "")))
    if not any(v for n, v in fields if n == no.get("name")):
        return None
    action = urllib.parse.urljoin(base, form["action"])
    return f"{action}?{urllib.parse.urlencode(fields)}"


def ongoing_folders(soup, base):
    """Circular folders listed under "Ongoing Circular" on the home page: [(text, folder)]."""
    boxes = [fs for fs in soup.find_all("fieldset")
             if fs.find("legend") and "ongoing" in fs.find("legend").get_text().lower()]
    if not boxes and not soup.find("legend"):
        boxes = [soup]  # page without the usual boxes: take every circular folder
    out = []
    for box in boxes:
        for a in box.find_all("a", href=True):
            folder = circular_folder(urllib.parse.urljoin(base, a["href"].strip()))
            if folder and host_of(folder) == host_of(base) and folder not in [f for _, f in out]:
                out.append((clean(a.get_text(" ", strip=True)), folder))
    return out


def read_deadline(text):
    """'[Deadline: 19 Dec 2026 04:59:59 PM]' -> '19 Dec 2026, 04:59 PM (30 days left)'"""
    m = re.search(r"Deadline\s*:?\s*([^\]\n]{6,40})", text, re.I)
    days = find_dates(m.group(1)) if m else []
    if not days:
        return None
    t = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?\s*([AP]M)", m.group(1), re.I)
    return f"{days[0]:%d %b %Y}" + (f", {t.group(1)} {t.group(2).upper()}" if t else "") \
        + days_left(days[0])


def read_apply_site(root):
    """All ongoing circulars of one apply site, each with its PDF, deadline, the link to its
    post list and every post's form link. None = the site could not be opened."""
    session = requests.Session()
    try:
        home, base = site_get(session, root)
    except (requests.exceptions.RequestException, RuntimeError):
        return None
    if "teletalk" not in str(home).lower():
        return None
    circulars = []
    for home_text, folder in ongoing_folders(home, base)[:6]:
        try:
            page, url = site_get(session, folder)
            text = page.get_text(" ", strip=True)
            folder_pdf = next((urllib.parse.urljoin(url, a["href"]) for a in page.find_all("a", href=True)
                               if is_pdf(urllib.parse.urljoin(url, a["href"]))), None)
            form_page = next((urllib.parse.urljoin(url, a["href"]) for a in page.find_all("a", href=True)
                              if "circulars.php" in a["href"]), folder + "circulars.php")
            deadline = read_deadline(text)
            page, url = site_get(session, form_page)
            for ref, posts_url, pdf in radio_form(page, url, "circular_id")[:6]:
                page, url = site_get(session, posts_url)
                posts = [{"label": label, "form": form}
                         for label, form, _ in radio_form(page, url, "post_code")]
                circulars.append({"ref": ref, "pdf": pdf or folder_pdf, "deadline": deadline,
                                  "posts_url": posts_url, "posts": posts, "folder": folder})
        except (requests.exceptions.RequestException, RuntimeError, KeyError) as e:
            print(f"    {folder}: {str(e)[:120]}")
            ref = re.sub(r"^Circular\s*Ref\s*:?\s*", "", home_text, flags=re.I)
            circulars.append({"ref": ref, "pdf": None, "deadline": None, "posts_url": folder,
                              "posts": [], "folder": folder})
    print(f"    {root}: {len(circulars)} ongoing circular(s), "
          f"{sum(len(c['posts']) for c in circulars)} posts")
    return circulars


def name_key(s):
    """Post name without spaces, brackets or dashes, for comparing."""
    s = unicodedata.normalize("NFC", s or "").lower()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in "LMN")


def match_posts(posts, site_posts):
    """alljobs post -> its form link on the apply site: {job id: form url}.
    Same name (English or Bangla) first; otherwise a very close name, but only when
    no other post comes near it (names like "প্রোগ্রামার" and "সহকারী প্রোগ্রামার" stay apart)."""
    keyed = []
    for sp in site_posts:
        m = re.match(r"(.*?)\s*\[(.*)\]\s*$", sp["label"])
        names = [m.group(1), m.group(2)] if m else [sp["label"]]
        keyed.append(([name_key(n) for n in names if len(name_key(n)) >= 3], sp["form"]))
    found = {}
    for p in posts:
        mine = [name_key(x) for x in (p.get("title_en"), p.get("title_bn"), p["title"])]
        mine = [x for x in mine if len(x) >= 3]
        exact = [form for keys, form in keyed if set(keys) & set(mine)]
        if exact:
            found[p["job"]] = exact[0]
            continue
        scores = sorted(((max((difflib.SequenceMatcher(None, a, b).ratio()
                               for a in mine for b in keys), default=0), form)
                         for keys, form in keyed), reverse=True)
        if scores and scores[0][0] >= 0.9 and (len(scores) < 2 or scores[0][0] - scores[1][0] >= 0.06):
            found[p["job"]] = scores[0][1]
    return found


FORM_LINKS = {}  # premium.php link -> application.php link (this run only)


def use_apply_site(item, circulars):
    """Fill the message from the apply site. False = nothing usable there."""
    if not circulars:
        return False
    posts = item.get("posts") or []
    best, best_map = None, {}
    for c in circulars:
        m = match_posts(posts, c["posts"])
        if len(m) > len(best_map):
            best, best_map = c, m
    if best is None and len(circulars) == 1:
        best = circulars[0]
    if best is None:  # several circulars and none matches: list them all
        root = apply_site_root(circulars[0]["folder"])
        item["links"] = [("🌐", "Apply site (all circulars)", root)]
        item["links"] += [("📝", "Circular Ref: " + c["ref"] if c["ref"] else c["folder"], c["posts_url"])
                          for c in circulars]
        return True

    if best["pdf"]:
        item["url"], item["url_label"], item["url_icon"] = best["pdf"], "Circular (PDF)", "📄"
    item["links"] = [("🌐", "All posts (apply site)", best["posts_url"])]
    item["info"] = list(item.get("info") or [])
    if best["ref"]:
        item["info"].append(f"📝 Circular Ref: {best['ref']}")
    if best["deadline"]:
        item["info"].append(f"⏰ Deadline: {best['deadline']}")
    for p in posts:
        # matched -> its own form ("No" already chosen on the premium page);
        # not matched -> the list of all posts of this circular
        form = best_map.get(p["job"])
        if form:
            if form not in FORM_LINKS:
                FORM_LINKS[form] = choose_no(form) or form
            form = FORM_LINKS[form]
        p["apply"] = form or best["posts_url"]
        p["no_chosen"] = bool(form) and form != best_map.get(p["job"])
        p["pdf"] = best["pdf"]
        p["form_found"] = p["job"] in best_map
    return True


def use_job_pages(item):
    """Fallback: links found on the alljobs job pages themselves."""
    posts = item.get("posts") or []
    pdfs = [p["pdf"] for p in posts if p.get("pdf")]
    if pdfs:
        item["url"], item["url_label"], item["url_icon"] = pdfs[0], "Circular (PDF)", "📄"
    for p in posts:
        p["form_found"] = bool(p.get("apply"))
    sites = [p["apply"] for p in posts if p.get("apply") and is_apply_site(p["apply"])]
    if sites:
        item["links"] = [("🌐", "Apply site", apply_site_root(sites[0]))]


def add_teletalk_links(to_send, seen):
    """For every Teletalk circular about to be sent: circular PDF, deadline, the list of
    all posts and each post's application form (from the organisation's apply site)."""
    groups = [it for src, it in to_send if src["type"] == "teletalk" and it.get("posts")]
    if not groups:
        return ""
    print("[Teletalk apply sites] looking for circular PDFs and application forms")
    sites = {}  # root -> circulars (this run only)

    def site(root):
        if root not in sites:
            sites[root] = read_apply_site(root)
        return sites[root]

    cache = seen.setdefault(LINK_CACHE, {})
    with JobPageReader(cache) as reader:
        for it in groups:
            try:
                code = re.sub(r"[^a-z0-9]", "", (it.get("short_name") or "").lower())
                if code and use_apply_site(it, site(f"https://{code}.teletalk.com.bd/")):
                    continue
                # Short name is not the sub-site's name: look at the alljobs job pages.
                for p in it["posts"]:
                    found = reader.links_for(p["org"], p["job"])
                    p["pdf"], p["apply"] = found.get("pdf"), found.get("apply")
                roots = {apply_site_root(p["apply"]) for p in it["posts"]
                         if p.get("apply") and is_apply_site(p["apply"])}
                if not any(use_apply_site(it, site(r)) for r in roots):
                    use_job_pages(it)
            except Exception as e:
                print(f"    {it['title'][:50]}: {str(e)[:150]}")
    if len(cache) > 3000:  # keep seen.json small
        seen[LINK_CACHE] = dict(list(cache.items())[-3000:])

    posts = [p for it in groups for p in it["posts"]]
    pdfs = sum(1 for it in groups if it.get("url_icon") == "📄")
    forms = sum(1 for p in posts if p.get("form_found"))
    skipped = sum(1 for p in posts if p.get("no_chosen"))
    note = (f"circular PDF found for {pdfs} of {len(groups)} circulars, "
            f"direct application form for {forms} of {len(posts)} posts "
            f"({skipped} with \"Premium member: No\" already chosen)")
    print("    " + note)
    return note


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
    icon = item.get("url_icon", "🔗")
    lines.append(f'{icon} <a href="{e(item["url"], quote=True)}">{e(item.get("url_label") or "Open")}</a>')
    for icon, label, link in item.get("links", []):
        lines.append(f'{icon} <a href="{e(link, quote=True)}">{e(label)}</a>')

    posts = item.get("posts") or []
    if posts:
        total = item.get("total_posts") or len(posts)
        head = f"Posts ({len(posts)})" if len(posts) >= total else f"New posts ({len(posts)} of {total})"
        lines += ["", f"<b>{head}:</b>"]
        for i, p in enumerate(posts[:MAX_POSTS], 1):
            # the post name opens its application form (or its job page if no form was found)
            link = p.get("apply") or p["url"]
            line = f'{i}. <a href="{e(link, quote=True)}">{e(p["title"])}</a>'
            if p.get("pdf") and p["pdf"] != item["url"]:  # this post has its own circular
                line += f' · <a href="{e(p["pdf"], quote=True)}">📄 PDF</a>'
            lines.append(line)
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

        # Keep the list from growing forever (oldest entries are dropped first).
        seen[name] = (old_list + fresh)[-5000:]

    found = len(to_send)
    if found > MAX_MESSAGES:
        print(f"WARNING: {found} circulars; sending only the first {MAX_MESSAGES}.")
        to_send = to_send[:MAX_MESSAGES]

    links_note = add_teletalk_links(to_send, seen)
    if links_note:
        report.append(f"📄 Teletalk: {links_note}")

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
