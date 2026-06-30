import os
import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Source ────────────────────────────────────────────────────────────────────
# National NHS Jobs service. Unlike the old Civica TRAC site (healthjobsuk.com),
# this is server-rendered and its sort sticks in the URL, so we can pull
# newest-first and usually only need page 1.
#   staffGroup=MEDICAL_AND_DENTAL -> only medical & dental posts (~2,300, not 13k)
#   sort=publicationDateDesc      -> newest jobs first (stable, no session needed)
#   page=N                        -> pagination, 10 jobs per page
BASE_URL = ("https://www.jobs.nhs.uk/candidate/search/results"
            "?staffGroup=MEDICAL_AND_DENTAL&sort=publicationDateDesc&language=en")

# Optional fallback: if NHS Jobs ever blocks GitHub Actions' IPs, set a free
# ScraperAPI key as the SCRAPER_API_KEY secret and requests route through it.
# Left unset = fetch directly (the normal, free path).
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# How many pages to grab when seeding an empty jobs.txt (first run only).
SEED_PAGES = 10
# Safety cap on pages walked in a single run.
MAX_PAGES = 40

# Senior grades — filtered out of notifications (but still tracked).
# Match is a case-insensitive substring on the job title.
EXCLUDE_TITLES = [
    "consultant",          # also catches "GP Consultant"
    "specialist",          # catches "Specialist" (SAS) and "Associate Specialist"
    "gp principal",
    "gp partner",
    "clinical director",
    "medical director",
]

def is_excluded(title: str) -> bool:
    title_lower = title.lower()
    return any(word in title_lower for word in EXCLUDE_TITLES)

# ── Page fetching ─────────────────────────────────────────────────────────────
def fetch_html(url: str):
    """Fetch a page. Returns (html, status)."""
    try:
        if SCRAPER_API_KEY:
            r = requests.get(
                "https://api.scraperapi.com/",
                params={"api_key": SCRAPER_API_KEY, "url": url},
                timeout=60,
            )
        else:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        return r.text, r.status_code
    except Exception as e:
        print(f"Fetch error for {url}: {e}")
        return "", 0

# ── Parse one results page ────────────────────────────────────────────────────
def parse_jobs(html: str):
    """Return jobs on this page, in page order (newest first).
    Each job: {ID, Title, Link}. ID is the NHS Jobs reference, e.g. C9325-26-0414."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen_on_page = set()
    for a in soup.select('a[href*="/candidate/jobadvert/"]'):
        href = a.get("href", "").split("?")[0].rstrip("/")
        if "/candidate/jobadvert/" not in href:
            continue
        ref = href.split("/")[-1]
        if not ref or ref in seen_on_page:
            continue
        seen_on_page.add(ref)
        title = a.get_text(strip=True)
        if not title:
            continue
        link = href if href.startswith("http") else f"https://www.jobs.nhs.uk{href}"
        jobs.append({"ID": ref, "Title": title, "Link": link})
    return jobs

# ── Telegram (best-effort, never raises) ──────────────────────────────────────
def telegram_send(message: str):
    """Send a Telegram message. Respects 429 retry_after once, then gives up."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 429:
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after", 30)
            except Exception:
                retry_after = 30
            if retry_after <= 90:
                print(f"Telegram rate limited; sleeping {retry_after}s then retrying once")
                time.sleep(retry_after + 1)
                response = requests.post(url, json=payload, timeout=10)
            else:
                print(f"Telegram rate limit too long ({retry_after}s) — skipping this message")
                return
        if response.status_code != 200:
            print(f"Telegram non-200: {response.status_code} {response.text[:200]}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

def notify_new_jobs(new_jobs):
    """Send ALL new job listings via Telegram, chunked under the 4096 char limit."""
    total = len(new_jobs)
    chunk_size = 8
    chunks = [new_jobs[i:i + chunk_size] for i in range(0, total, chunk_size)]

    for idx, chunk in enumerate(chunks):
        i = idx * chunk_size
        header = f"\U0001F3E5 New NHS Jobs ({i+1}-{min(i+chunk_size, total)} of {total})\n\n"
        body_lines = [f"• {job['Title']}\n  {job['Link']}" for job in chunk]
        telegram_send(header + "\n\n".join(body_lines))
        if idx < len(chunks) - 1:
            time.sleep(3)

# ── Load/save seen job IDs ────────────────────────────────────────────────────
def load_previous_job_ids():
    try:
        with open("jobs.txt", "r") as f:
            # ignore blank lines so an "emptied" file still counts as empty
            return set(line for line in f.read().splitlines() if line.strip())
    except FileNotFoundError:
        return set()

def save_current_job_ids(job_ids):
    with open("jobs.txt", "w") as f:
        f.write("\n".join(sorted(job_ids)))

# ── Failure-state flag (alert cooldown) ───────────────────────────────────────
FAILURE_FLAG = ".scraper_failing"

def is_in_failure_state():
    return os.path.exists(FAILURE_FLAG)

def mark_failure_state():
    with open(FAILURE_FLAG, "w") as f:
        f.write(datetime.utcnow().isoformat() + "Z\n")

def clear_failure_state():
    if os.path.exists(FAILURE_FLAG):
        os.remove(FAILURE_FLAG)

# ── Scrape newest jobs ────────────────────────────────────────────────────────
def scrape_new_jobs(previous_ids):
    """Walk pages newest-first, collecting jobs not seen before. Because the
    list is sorted newest-first, we stop as soon as we hit a job we've already
    seen — everything past it is older. Returns (new_jobs, all_ids_seen, ok).
    ok=False means a fetch failed and the caller must NOT overwrite jobs.txt."""
    new_jobs = []
    ids_this_run = set()
    page = 1

    while page <= MAX_PAGES:
        url = f"{BASE_URL}&page={page}"
        html, status = fetch_html(url)
        if status != 200 or not html:
            print(f"Failed to fetch page {page}: HTTP {status}")
            return [], set(), False

        page_jobs = parse_jobs(html)
        if not page_jobs:
            print(f"Page {page} had no jobs — stopping.")
            break

        hit_seen = False
        for job in page_jobs:
            ids_this_run.add(job["ID"])
            if job["ID"] in previous_ids:
                hit_seen = True
                break
            new_jobs.append(job)

        print(f"Page {page}: {len(page_jobs)} jobs, "
              f"{len(new_jobs)} new so far"
              + (" (reached already-seen jobs — stopping)" if hit_seen else ""))

        if hit_seen or len(page_jobs) < 10:
            break
        page += 1
        time.sleep(0.5)

    return new_jobs, ids_this_run, True

def seed_silently():
    """First run with an empty jobs.txt: record the newest SEED_PAGES pages of
    IDs without notifying, so we don't spam thousands of existing jobs."""
    seeded = set()
    for page in range(1, SEED_PAGES + 1):
        url = f"{BASE_URL}&page={page}"
        html, status = fetch_html(url)
        if status != 200 or not html:
            print(f"Seed: failed to fetch page {page}: HTTP {status}")
            return seeded, False
        page_jobs = parse_jobs(html)
        if not page_jobs:
            break
        seeded.update(job["ID"] for job in page_jobs)
        time.sleep(0.5)
    return seeded, True

# ── Main ──────────────────────────────────────────────────────────────────────
def monitor():
    if SCRAPER_API_KEY:
        print("Fetching via ScraperAPI")
    else:
        print("Fetching jobs.nhs.uk directly")

    previous_ids = load_previous_job_ids()
    print(f"Loaded {len(previous_ids)} previously seen job IDs")

    # First-ever run (empty jobs.txt): seed silently, don't spam ~13,000 jobs.
    if not previous_ids:
        seeded, ok = seed_silently()
        if not ok:
            print("Seeding failed — will retry next run, no state written.")
            return
        save_current_job_ids(seeded)
        clear_failure_state()
        print(f"First run — seeded {len(seeded)} jobs without notifications.")
        return

    new_jobs, ids_this_run, ok = scrape_new_jobs(previous_ids)

    if not ok:
        if is_in_failure_state():
            print("Scraper still failing. Alert already sent — staying silent.")
        else:
            msg = (
                "⚠️ NHS scraper failing (fetch errors from jobs.nhs.uk) — all "
                "retries failed. Tracked state preserved. You'll get one "
                "'recovered' message when it's working again."
            )
            print(msg)
            telegram_send(msg)
            mark_failure_state()
        return

    if is_in_failure_state():
        telegram_send("✅ NHS scraper recovered and is working again.")
        clear_failure_state()

    print(f"{len(new_jobs)} new job(s) found")

    notification_jobs = [job for job in new_jobs if not is_excluded(job["Title"])]
    senior_filtered = len(new_jobs) - len(notification_jobs)

    if notification_jobs:
        print(f"Notifying about {len(notification_jobs)} non-senior job(s) "
              f"({senior_filtered} senior jobs filtered out)")
        notify_new_jobs(notification_jobs)
    elif new_jobs:
        print(f"No new non-senior jobs ({senior_filtered} senior jobs filtered out)")
    else:
        print("No new jobs found")

    # Persist: add the newly seen IDs to the tracked set.
    if new_jobs:
        save_current_job_ids(previous_ids | ids_this_run)

if __name__ == "__main__":
    monitor()
    print("Done")
