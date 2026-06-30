# ROMANCE READER EMAIL ROBOT - SELF-SUSTAINING ENGINE
# Layer 1: URL TTL        — URLs expire after 7 days, get revisited weekly
# Layer 2: Daily modifier — rotating search terms, fresh DDG results each day
# Layer 3: Auto-keywords  — 1.68M combinatorial pool, 500 selected per day via date-seed
# Layer 4: Blog targeting — blogspot.com + wordpress.com per keyword
# Layer 5: Email dorking  — "gmail.com/yahoo/hotmail" + reader terms → email in snippet

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
import time
import re
import os
import hashlib
from datetime import datetime, timedelta
import json
import random
from fake_useragent import UserAgent

# Detect GitHub Actions environment
IS_GITHUB_ACTIONS = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
BATCH = int(os.environ.get('BATCH', '0'))  # 0 = run all (local), 1-6 = batch

# ============================================
# CONSTANTS
# ============================================

TRACKER_FILE      = "last_run.json"
VISITED_URLS_FILE = "visited_urls.json"
MASTER_EMAILS_FILE = "master_emails.txt"
YIELD_TRACKER_FILE = "yield_tracker.json"
URL_TTL_DAYS      = 7    # revisit URLs after 7 days (weekly cycle = sustainable yield)
KEYWORDS_PER_DAY  = 500  # Full production mode

# Adaptive engine thresholds
TARGET_DAILY      = 750   # emails/day target
DROP_L1           = 0.30  # 30% drop → expand keyword pool
DROP_L2           = 0.50  # 50% drop → add new platforms to dork
DROP_L3           = 0.70  # 70% drop → purge stale cache + max dork volume

# 4 DDG regions — full geographic coverage
DDG_REGIONS = ['us-en', 'uk-en', 'au-en', 'ca-en']

# Daily rotating search modifier — different DDG results each day of week
DAILY_MODIFIERS = [
    'reviews',          # Monday
    'recommendations',  # Tuesday
    'blog',             # Wednesday
    'community',        # Thursday
    'contact',          # Friday
    'newsletter',       # Saturday
    'group',            # Sunday
]

# Romance blog directories — bypasses search engine entirely
BLOG_DIRECTORIES = [
    "https://blog.feedspot.com/romance_book_blogs/",
    "https://blog.feedspot.com/romance_book_review_blogs/",
    "https://alltop.com/romance",
    "https://www.thebookbloggerdirectory.com/",
    "https://www.bookbloggerlist.com/",
]

# ============================================
# PROXY & USER AGENT
# ============================================

import threading

_proxy_env = os.environ.get('PROXY_LIST', '')
PROXY_LIST = [p.strip().rstrip('/') for p in _proxy_env.split(',') if p.strip()]
_PROXY_LOCK = threading.Lock()           # guards all PROXY_LIST mutations
SKIP_DDG_NO_PROXY = False               # Set True at startup if 0 proxies found
PROXY_DEPLETED = False                  # Set True mid-run if pool drops to 0

_token_env = os.environ.get('GITHUB_TOKENS', '')
GITHUB_TOKENS = [t.strip() for t in _token_env.split(',') if t.strip()]

# ============================================
# FREE PROXY AUTO-FETCH (runs at startup if no paid proxies)
# ============================================
# Strategy: 13 sources → ~3,000-8,000 candidates
# Parallel testing (50 threads, 2s timeout) → ~1,125 proxies tested in 45s
# At 3-5% success rate → ~33-56 working proxies reliably
# HARD RULE: NEVER use GitHub raw IP for DDG — skip batch if 0 proxies found

from concurrent.futures import ThreadPoolExecutor, as_completed

FREE_PROXY_SOURCES = [
    # API sources (largest lists)
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http",
    # GitHub maintained lists (most reliable uptime)
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt",
]

def _fetch_free_proxies():
    """Download raw proxy lists from all sources in parallel."""
    raw = []
    def _fetch_one(source):
        try:
            r = requests.get(source, timeout=4)
            # geonode returns JSON, others return plain text
            if 'geonode' in source:
                data = json.loads(r.text)
                return [item['ip'] + ':' + item['port'] for item in data.get('data', [])]
            return [p.strip() for p in r.text.strip().splitlines() if p.strip()]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=13) as ex:
        for result in ex.map(_fetch_one, FREE_PROXY_SOURCES):
            raw.extend(result)

    # Deduplicate and format as http://IP:PORT
    seen = set()
    proxies = []
    for p in raw:
        p = p.strip()
        if ':' in p and p not in seen:
            seen.add(p)
            proxies.append('http://' + p if not p.startswith('http') else p)
    return proxies

def _test_proxy(proxy):
    """Fast liveness check — single URL, 2s timeout.
    Dead proxies fail in <0.3s (connection refused) — 2s is generous for live ones."""
    try:
        r = requests.get('http://ip-api.com/json',
                         proxies={'http': proxy, 'https': proxy},
                         timeout=2)
        return r.status_code == 200
    except Exception:
        return False

def _load_working_free_proxies(target=80, time_limit=45):
    """
    Fetch + parallel-test proxies within time_limit seconds.
    50 threads × 2s timeout → ~1,125 proxies tested in 45s.
    At 5% success rate → ~56 working proxies.
    At 3% success rate → ~33 working proxies.
    Returns up to target working proxies.
    """
    print("  Fetching proxy lists from " + str(len(FREE_PROXY_SOURCES)) + " sources in parallel...")
    raw = _fetch_free_proxies()
    random.shuffle(raw)
    # Test at most 1,200 candidates — 50 threads × 45s ÷ 2s = 1,125 tested
    candidates = raw[:1200]
    print("  " + str(len(raw)) + " candidates found — parallel-testing " + str(len(candidates)) + " (max 45s, 50 threads)...")

    working = []
    start = time.time()
    tested = 0

    executor = ThreadPoolExecutor(max_workers=50)
    futures = {executor.submit(_test_proxy, p): p for p in candidates}
    try:
        for future in as_completed(futures):
            if time.time() - start > time_limit or len(working) >= target:
                break
            tested += 1
            try:
                if future.result():
                    working.append(futures[future])
            except Exception:
                pass
            if tested % 200 == 0:
                print("  Tested " + str(tested) + " | Working: " + str(len(working)) + " | " +
                      str(int(time.time() - start)) + "s elapsed")
    finally:
        # cancel_futures=True (Python 3.9+) kills queued threads instantly — no waiting
        executor.shutdown(wait=False, cancel_futures=True)

    elapsed = int(time.time() - start)
    print("  RESULT: " + str(len(working)) + " working proxies found in " + str(elapsed) + "s")

    # DDG smoke test — confirm at least one proxy reaches DDG over HTTPS
    if working:
        ddg_ok = False
        for p in working[:5]:
            try:
                r = requests.get('https://duckduckgo.com/',
                                 proxies={'http': p, 'https': p}, timeout=5)
                if r.status_code == 200:
                    ddg_ok = True
                    break
            except Exception:
                continue
        print("  DDG HTTPS via proxy: " + ("YES — safe to scrape" if ddg_ok else "NO — DDG may block these proxies"))
    return working

def get_next_proxy():
    """Thread-safe proxy selection. Returns None if pool is empty."""
    with _PROXY_LOCK:
        if not PROXY_LIST:
            return None
        return random.choice(PROXY_LIST)

def _init_proxy_list():
    """
    Called once at startup. Auto-fetch free proxies if no paid ones.
    HARD RULE: if no proxies found, set a global flag to skip DDG entirely.
    GitHub's raw IP must NEVER be used for DDG — it will get blacklisted.
    """
    global PROXY_LIST, SKIP_DDG_NO_PROXY
    SKIP_DDG_NO_PROXY = False
    if PROXY_LIST:
        return  # paid proxies present — use them
    if IS_GITHUB_ACTIONS:
        free = _load_working_free_proxies(target=80, time_limit=45)
        if free:
            PROXY_LIST.extend(free)
            print("  PROXY_LIST: " + str(len(PROXY_LIST)) + " free proxies loaded — DDG scraping enabled")
        else:
            SKIP_DDG_NO_PROXY = True
            print("  CRITICAL: 0 working proxies — DDG keyword phase SKIPPED to protect GitHub IP")
            print("  Only dork engine (proxy-required mode) and blog directories will run")

try:
    _ua = UserAgent()
    def get_random_user_agent():
        return _ua.random
except Exception:
    def get_random_user_agent():
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

# ============================================
# DIAGNOSTICS
# ============================================

def print_startup_diagnostics():
    print("=" * 60)
    print("DIAGNOSTICS:")
    if PROXY_LIST:
        p = PROXY_LIST[0]
        parts = p.split('@')
        masked = parts[0].split(':')[0] + ':****@' + parts[1] if len(parts) == 2 else p[:20]
        print("  PROXY_LIST      : YES - " + str(len(PROXY_LIST)) + " proxies (" + masked + ")")
    else:
        print("  PROXY_LIST      : NO - DDG will block GitHub IP. Set secret.")
    print("  DDG regions     : " + str(DDG_REGIONS))
    print("  Daily modifier  : " + get_daily_modifier())
    print("  Batch           : " + str(BATCH))
    print("  URL TTL         : " + str(URL_TTL_DAYS) + " days")
    print("=" * 60)

# ============================================
# URL TTL SYSTEM (Layer 1)
# ============================================

def load_visited_urls():
    if not os.path.exists(VISITED_URLS_FILE):
        return {}
    try:
        with open(VISITED_URLS_FILE, 'r') as f:
            data = json.load(f)
        # Migrate old list format → dict format
        if isinstance(data, list):
            old_date = (datetime.now() - timedelta(days=URL_TTL_DAYS)).strftime('%Y-%m-%d')
            print("  Migrating visited_urls to TTL format...")
            return {url: old_date for url in data}
        return data
    except Exception:
        return {}

def is_url_stale(visited_dict, url):
    """
    Returns True if URL was visited RECENTLY (within TTL) → should be SKIPPED.
    Returns False if never visited OR TTL has expired → safe to visit again.
    NOTE: 'stale' here means 'too fresh to revisit' — skip when True.
    """
    if url not in visited_dict:
        return False  # never visited — process it
    try:
        visited_date = datetime.strptime(visited_dict[url], '%Y-%m-%d')
        age_days = (datetime.now() - visited_date).days
        return age_days < URL_TTL_DAYS  # True = visited within 7 days = skip it
    except Exception:
        return False

def mark_visited(visited_dict, url):
    visited_dict[url] = datetime.now().strftime('%Y-%m-%d')

def save_visited_urls(visited_dict):
    try:
        with open(VISITED_URLS_FILE, 'w') as f:
            json.dump(visited_dict, f)
    except Exception:
        pass

def count_fresh_urls(visited_dict):
    """Count URLs eligible for revisiting (older than TTL)."""
    today = datetime.now()
    expired = 0
    for date_str in visited_dict.values():
        try:
            age = (today - datetime.strptime(date_str, '%Y-%m-%d')).days
            if age >= URL_TTL_DAYS:
                expired += 1
        except Exception:
            pass
    return expired

# ============================================
# MASTER EMAIL LIST
# ============================================

def load_master_emails():
    if not os.path.exists(MASTER_EMAILS_FILE):
        return set()
    try:
        with open(MASTER_EMAILS_FILE, 'r') as f:
            return set(line.strip() for line in f if line.strip())
    except Exception:
        return set()

def save_master_emails(new_emails):
    existing = load_master_emails()
    combined = existing | set(new_emails)
    try:
        with open(MASTER_EMAILS_FILE, 'w') as f:
            for email in sorted(combined):
                f.write(email + '\n')
    except Exception:
        pass
    return len(combined) - len(existing)


# ============================================
# ADAPTIVE YIELD ENGINE
# Monitors daily email yield and auto-expands
# sources when a drop is detected.
# ============================================

def load_yield_tracker():
    if not os.path.exists(YIELD_TRACKER_FILE):
        return {'daily_yields': [], 'baseline': 0, 'expansion_level': 0}
    try:
        with open(YIELD_TRACKER_FILE) as f:
            return json.load(f)
    except Exception:
        return {'daily_yields': [], 'baseline': 0, 'expansion_level': 0}

def save_yield_tracker(tracker):
    try:
        with open(YIELD_TRACKER_FILE, 'w') as f:
            json.dump(tracker, f, indent=2)
    except Exception:
        pass

def record_batch_yield(new_emails):
    """Record how many NEW unique emails this batch found."""
    tracker = load_yield_tracker()
    today = datetime.now().strftime('%Y-%m-%d')

    # Accumulate daily total across all 6 batches
    if not tracker.get('today_date') or tracker['today_date'] != today:
        # New day — push yesterday's total into history
        if tracker.get('today_total', 0) > 0:
            tracker['daily_yields'].append(tracker['today_total'])
            tracker['daily_yields'] = tracker['daily_yields'][-30:]  # keep 30 days
        tracker['today_date'] = today
        tracker['today_total'] = 0

    tracker['today_total'] = tracker.get('today_total', 0) + new_emails

    # Set baseline from first 3 complete days
    if len(tracker['daily_yields']) == 3 and tracker.get('baseline', 0) == 0:
        tracker['baseline'] = sum(tracker['daily_yields']) / 3
        print("  YIELD BASELINE SET: " + str(int(tracker['baseline'])) + " emails/day")

    save_yield_tracker(tracker)
    return tracker

def get_expansion_level():
    """
    Compare recent 3-day average to baseline.
    Returns expansion level (0-3) needed.
    0 = normal, 1 = keyword expand, 2 = new platforms, 3 = full expansion + cache purge
    """
    tracker = load_yield_tracker()
    yields = tracker.get('daily_yields', [])
    baseline = tracker.get('baseline', 0)

    if len(yields) < 3 or baseline == 0:
        return 0  # not enough history yet

    recent_avg = sum(yields[-3:]) / 3
    drop = (baseline - recent_avg) / baseline

    current_level = tracker.get('expansion_level', 0)

    if drop >= DROP_L3 and current_level < 3:
        new_level = 3
    elif drop >= DROP_L2 and current_level < 2:
        new_level = 2
    elif drop >= DROP_L1 and current_level < 1:
        new_level = 1
    else:
        new_level = current_level

    if new_level > current_level:
        print("\n  ADAPTIVE ENGINE: yield dropped " + str(int(drop * 100)) +
              "% — triggering expansion level " + str(new_level))
        tracker['expansion_level'] = new_level
        save_yield_tracker(tracker)

    return new_level

def apply_expansion(level):
    """
    Level 1 (30% drop): Pull more keywords per day (+200)
    Level 2 (50% drop): Add Wattpad/Royal Road/Tumblr to dork queries
    Level 3 (70% drop): Purge oldest 40% of visited_urls cache + max dork volume
    Each level is cumulative — level 3 includes levels 1 and 2.
    """
    global KEYWORDS_PER_DAY

    if level >= 1:
        KEYWORDS_PER_DAY = min(KEYWORDS_PER_DAY + 200, 1000)
        print("  EXPAND L1: keywords → " + str(KEYWORDS_PER_DAY) + "/day")

    if level >= 2:
        print("  EXPAND L2: adding Wattpad / Royal Road / Tumblr to dork pool")
        # Injected into generate_dork_queries at runtime via module-level flag
        globals()['DORK_EXTRA_PLATFORMS'] = True

    if level >= 3:
        print("  EXPAND L3: purging oldest 40% of URL cache to force re-discovery")
        _purge_old_cache(keep_pct=0.60)

def _purge_old_cache(keep_pct=0.60):
    """Remove the oldest (keep_pct)% of visited_urls entries so pages get re-crawled."""
    visited = load_visited_urls()
    if not visited:
        return
    # Sort by date ascending (oldest first)
    sorted_urls = sorted(visited.items(), key=lambda x: x[1])
    keep_count = int(len(sorted_urls) * keep_pct)
    kept = dict(sorted_urls[len(sorted_urls) - keep_count:])
    save_visited_urls(kept)
    print("  Cache purged: " + str(len(visited) - len(kept)) + " old URLs removed → " +
          str(len(kept)) + " retained")


# ============================================
# EMAIL FINDING
# ============================================

def find_emails(text):
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(email_pattern, text)

    obfuscated = re.findall(
        r'[a-zA-Z0-9._%+-]+\s*[\[\(]?at[\]\)]?\s*[a-zA-Z0-9.-]+\s*[\[\(]?dot[\]\)]?\s*[a-zA-Z]{2,}',
        text, re.IGNORECASE
    )
    for match in obfuscated:
        cleaned = re.sub(r'\s*[\[\(]?at[\]\)]?\s*', '@', match, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*[\[\(]?dot[\]\)]?\s*', '.', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        if '@' in cleaned:
            emails.append(cleaned)

    return emails

def clean_emails(email_list):
    email_list = list(set(email_list))

    blocked_local_exact = {
        'admin', 'webmaster', 'noreply', 'no-reply', 'donotreply',
        'support', 'help', 'info', 'contact', 'sales', 'marketing',
        'press', 'media', 'editor', 'editors', 'pr', 'ceo', 'cfo',
        'cto', 'founder', 'hello', 'team', 'staff', 'office'
    }

    blocked_domains_exact = {
        'example.com', 'test.com', 'sentry.io', 'amazonaws.com',
        'cloudflare.com', 'noreply.github.com', 'users.noreply.github.com'
    }

    clean_list = []
    for email in email_list:
        if '@' not in email:
            continue
        parts = email.lower().split('@')
        if len(parts) != 2:
            continue
        local, domain = parts[0], parts[1]
        if local in blocked_local_exact:
            continue
        if domain in blocked_domains_exact:
            continue
        clean_list.append(email)

    return clean_list

# ============================================
# DAILY MODIFIER (Layer 2)
# ============================================

def get_daily_modifier():
    return DAILY_MODIFIERS[datetime.now().weekday()]

# ============================================
# AUTO-KEYWORD GENERATOR (Layer 3)
# ============================================

# ============================================
# 2.5M COMBINATORIAL KEYWORD ENGINE
# Pool = KW_SUBGENRES × KW_ACTIVITIES × KW_MODIFIERS
# No list stored — keywords computed from index on the fly
# At 500/day: 13.7 years to exhaust the full pool
# ============================================

KW_SUBGENRES = [
    # Core romance subgenres
    'paranormal romance', 'regency romance', 'military romance',
    'billionaire romance', 'small town romance', 'highland romance',
    'mafia romance', 'reverse harem romance', 'sports romance',
    'rockstar romance', 'office romance', 'romantic suspense',
    'shifter romance', 'vampire romance', 'fantasy romance',
    'cozy romance', 'dark romance', 'historical romance',
    'contemporary romance', 'spicy romance', 'steamy romance',
    'viking romance', 'cowboy romance', 'werewolf romance',
    'alien romance', 'monster romance', 'fae romance',
    'dragon romance', 'witch romance', 'mermaid romance',
    'angel romance', 'demon romance', 'zombie romance',
    'time travel romance', 'space romance', 'sci-fi romance',
    'dystopian romance', 'post-apocalyptic romance', 'gothic romance',
    'Southern romance', 'beach romance', 'island romance',
    'mountain romance', 'city romance', 'coastal romance',
    'holiday romance', 'Christmas romance', 'summer romance',
    'winter romance', 'autumn romance', 'spring romance',
    'new adult romance', 'coming of age romance', 'college romance',
    'high school romance', 'academy romance', 'campus romance',
    'royal romance', 'princess romance', 'duke romance',
    'Highlander romance', 'Scottish romance', 'Irish romance',
    'Italian romance', 'French romance', 'Greek romance',
    'Russian romance', 'Bratva romance', 'cartel romance',
    'motorcycle club romance', 'MC romance', 'biker romance',
    'bodyguard romance', 'celebrity romance', 'athlete romance',
    'football romance', 'basketball romance', 'hockey romance',
    'baseball romance', 'MMA romance', 'boxer romance',
    'soccer romance', 'swimmer romance', 'surfer romance',
    'firefighter romance', 'police romance', 'doctor romance',
    'nurse romance', 'lawyer romance', 'CEO romance',
    'professor romance', 'teacher romance', 'coach romance',
    'chef romance', 'musician romance', 'artist romance',
    'single dad romance', 'single mom romance', 'opposites attract romance', 'forbidden boss romance',
    'age gap romance', 'taboo romance', 'obsession romance',
    'stalker romance', 'psycho romance', 'anti-hero romance',
    'villain romance', 'morally grey romance', 'dark hero romance',
    'omegaverse romance', 'dark fantasy romance', 'portal fantasy romance',
    'urban fantasy romance', 'paranormal mystery romance',
    'romantic comedy', 'rom-com', 'light romance', 'sweet romance',
    'clean romance', 'inspirational romance', 'Christian romance',
    'interracial romance', 'multicultural romance', 'LGBTQ romance',
    'MM romance', 'FF romance', 'bisexual romance', 'non-binary romance',
    'Colleen Hoover style romance', 'BookTok romance',
    'dark contemporary romance', 'grumpy hero romance', 'sunshine hero romance',
    'brooding hero romance', 'tortured hero romance', 'playboy romance',
    # Tropes as subgenres
    'enemies to lovers romance', 'slow burn romance', 'grumpy sunshine romance',
    'fake dating romance', 'second chance romance', 'forbidden romance',
    'forced proximity romance', 'best friends to lovers romance',
    'secret romance', 'marriage of convenience romance',
    'brother best friend romance', 'best friend brother romance',
    'one night stand romance', 'secret baby romance', 'pregnancy romance',
    'arranged marriage romance', 'forced marriage romance',
    'friends with benefits romance', 'love triangle romance',
    'childhood sweethearts romance', 'reunion romance',
    'workplace romance', 'forbidden love romance', 'star-crossed romance',
    'mistaken identity romance', 'road trip romance', 'vacation romance',
    'pen pal romance', 'online romance', 'long distance romance',
    'small town girl romance', 'city girl country boy romance',
    'rich boy poor girl romance', 'opposites romance', 'hate to love romance',
    'found family romance', 'reverse grumpy sunshine romance',
]

KW_ACTIVITIES = [
    # What readers do / how they identify
    'readers', 'book club members', 'reading community', 'fan community',
    'reading group', 'reader group', 'avid readers', 'book lovers',
    'bookworms', 'voracious readers', 'enthusiastic readers',
    'passionate readers', 'dedicated readers', 'fans',
    'book club', 'reading club', 'online book club', 'virtual book club',
    'arc readers', 'beta readers', 'advance readers', 'early readers',
    'review team', 'street team', 'ARC team', 'reader team',
    'newsletter subscribers', 'mailing list', 'email list',
    'book blog readers', 'blog followers', 'community members',
    'bookstagram followers', 'BookTok followers', 'goodreads members',
    'wattpad readers', 'reading challenge participants', 'buddy readers',
    'book swap members', 'book exchange members', 'reading partners',
    'fan group', 'discussion group', 'forum members', 'chat group',
    'series readers', 'audiobook listeners', 'ebook readers',
    'kindle readers', 'library members', 'subscription box subscribers',
    'book review bloggers', 'book bloggers', 'book influencers',
    'bookstagrammers', 'book reviewers', 'book recommenders',
    'reading buddies', 'book pen pals', 'reading challenge members',
    'book haul community', 'TBR community', 'romance book fans',
    'romance enthusiasts', 'romance addicts', 'romance obsessed',
    'nurses who read', 'teachers who read', 'moms who read',
    'working women who read', 'college students who read',
    'binge readers', 'one sitting readers', 'speed readers',
    'slow readers', 'weekend readers', 'late night readers',
    'kindle unlimited subscribers', 'audiobook club members',
    'book unboxing community', 'book subscription members',
]

KW_MODIFIERS = [
    # Geographic
    'USA', 'UK', 'Canada', 'Australia', 'Nigeria', 'South Africa',
    'Kenya', 'Ghana', 'Ireland', 'New Zealand', 'Jamaica', 'Philippines',
    'India', 'Singapore', 'Malaysia', 'Trinidad', 'Zimbabwe', 'Uganda',
    'Tanzania', 'Zambia', 'Botswana', 'Namibia', 'Rwanda', 'Ethiopia',
    'Pakistan', 'Sri Lanka', 'Bangladesh', 'Indonesia', 'Thailand',
    'Vietnam', 'Japan', 'South Korea', 'China', 'Taiwan', 'Hong Kong',
    'Germany', 'France', 'Spain', 'Italy', 'Netherlands', 'Sweden',
    'Norway', 'Denmark', 'Finland', 'Poland', 'Brazil', 'Mexico',
    'Argentina', 'Colombia', 'Chile', 'Peru', 'Venezuela',
    'Texas', 'California', 'New York', 'Florida', 'Georgia',
    'London', 'Lagos', 'Nairobi', 'Accra', 'Johannesburg', 'Cape Town',
    'Toronto', 'Sydney', 'Melbourne', 'Auckland', 'Dublin', 'Edinburgh',
    # Platform/context
    'blogspot', 'wordpress', 'goodreads', 'wattpad', 'bookbub',
    'tumblr', 'librarything', 'reddit', 'facebook group', 'instagram',
    'email list', 'newsletter', 'blog', 'forum', 'community',
    # Year modifiers
    '2024', '2025', '2026',
    # Context modifiers
    'contact', 'email', 'gmail', 'yahoo', 'hotmail',
    'join', 'subscribe', 'sign up', 'connect', 'reach out',
    'recommendations', 'reviews', 'favorites', 'top picks', 'must reads',
    'book haul', 'TBR', 'reading list', 'wish list', 'series',
    'buddy read', 'reading challenge', 'book swap', 'giveaway',
    'ARC', 'beta read', 'review request', 'street team',
    'discussion', 'chat', 'meet', 'network', 'group',
]

# ── Combinatorial pool size ───────────────────────────────────────────
_KW_TOTAL = len(KW_SUBGENRES) * len(KW_ACTIVITIES) * len(KW_MODIFIERS)

def _index_to_keyword(idx):
    """Convert flat index → 3-component keyword string. Zero memory, instant."""
    mod_i = idx % len(KW_MODIFIERS)
    act_i = (idx // len(KW_MODIFIERS)) % len(KW_ACTIVITIES)
    sub_i = idx // (len(KW_MODIFIERS) * len(KW_ACTIVITIES))
    return KW_SUBGENRES[sub_i] + ' ' + KW_ACTIVITIES[act_i] + ' ' + KW_MODIFIERS[mod_i]

def get_daily_keywords():
    """
    Draw KEYWORDS_PER_DAY keywords from the 2.5M combinatorial space.
    Date-seeded: same date = same full set. Different every day for ~13.7 years.
    Zero memory footprint — each keyword is computed from its index.
    In GitHub Actions: each batch gets its own 1/6 non-overlapping slice so
    all 6 batches cover different keywords — no duplicate work across the day.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    seed  = int(hashlib.md5(today.encode()).hexdigest(), 16)
    rng   = random.Random(seed)

    n = min(KEYWORDS_PER_DAY, _KW_TOTAL)
    indices  = rng.sample(range(_KW_TOTAL), n)
    keywords = [_index_to_keyword(i) for i in indices]

    # Slice into batch-specific segment — each of 6 batches gets ~84 unique keywords
    if IS_GITHUB_ACTIONS and BATCH > 0:
        batch_size = max(1, n // 6)
        start = (BATCH - 1) * batch_size
        end   = start + batch_size if BATCH < 6 else n  # Batch 6 gets remainder
        keywords = keywords[start:end]
        print("  Keyword pool    : {:,} combinatorial ({} × {} × {})".format(
            _KW_TOTAL, len(KW_SUBGENRES), len(KW_ACTIVITIES), len(KW_MODIFIERS)))
        print("  Batch {}/6 slice : keywords {:,}–{:,} ({} unique keywords this run)".format(
            BATCH, start + 1, end, len(keywords)))
    else:
        print("  Keyword pool    : {:,} combinatorial ({} × {} × {})".format(
            _KW_TOTAL, len(KW_SUBGENRES), len(KW_ACTIVITIES), len(KW_MODIFIERS)))
        print("  Selected today  : {} (date-seeded, exhausted in {:,} days)".format(
            n, _KW_TOTAL // n))

    return keywords


# ============================================
# SEARCH (multi-region + modifier + blogs)
# ============================================

def ddg_search(query, region, num_results, retry):
    results = []
    seen = set()
    attempt = 0
    while attempt < retry:
        try:
            proxy = get_next_proxy()
            with DDGS(proxy=proxy) as ddgs:
                for r in ddgs.text(query, max_results=num_results, region=region):
                    url = r['href']
                    if url not in seen:
                        seen.add(url)
                        results.append(url)
            break
        except Exception as e:
            attempt += 1
            print("  DDG error (" + region + ", attempt " + str(attempt) + "): " + str(e)[:60])
            time.sleep(random.uniform(2, 4))
    return results

def search_google(keyword, num_results=10, retry=3):
    # HARD BLOCK: no proxy at startup OR pool depleted mid-run → never hit DDG bare
    if SKIP_DDG_NO_PROXY or PROXY_DEPLETED:
        return []
    print("  Searching: " + keyword)
    all_results = []
    seen = set()

    regions = ['us-en', 'uk-en']
    for region in regions:
        for url in ddg_search(keyword, region, num_results, retry):
            if url not in seen:
                seen.add(url)
                all_results.append(url)
        time.sleep(random.uniform(0.5, 1))

    # 1 blog-specific search — personal reader blogs
    blog_query = keyword + ' readers site:blogspot.com OR site:wordpress.com'
    for url in ddg_search(blog_query, 'us-en', num_results, retry):
        if url not in seen:
            seen.add(url)
            all_results.append(url)
    time.sleep(random.uniform(3, 5) if not PROXY_LIST else random.uniform(0.5, 1))

    if len(all_results) == 0:
        print("  WARNING: 0 results — proxy may be blocked or PROXY_LIST empty")
    else:
        print("  Found " + str(len(all_results)) + " URLs")
    return all_results

# ============================================
# EMAIL DORK ENGINE (Layer 5)
# ============================================

def generate_dork_queries():
    """
    Tiered email dork queries ranked by likelihood of email appearing in DDG snippet.

    TIER 1: Reader intentionally posted their email (book swap, ARC, beta, contact)
    TIER 2: Personal blog contact sections (blogspot/wordpress)
    TIER 3: Country TLD site: filters (precise, avoids retailers/publishers)
    TIER 4: Country keyword matrix (broadest coverage, lowest density)
    """

    # TIER 1: Reader explicitly shared their email for a purpose
    tier1 = [
        # Book swap
        '"gmail.com" "romance book swap"',
        '"yahoo.com" "romance book swap"',
        '"gmail.com" "romance book exchange"',
        '"@gmail.com" "romance book swap"',
        # ARC readers
        '"gmail.com" "romance arc reader"',
        '"gmail.com" "romance advance reader"',
        '"@gmail.com" "romance arc reader"',
        '"gmail.com" "arc" "romance reader"',
        # Beta readers
        '"gmail.com" "romance beta reader"',
        '"@gmail.com" "romance beta reader"',
        '"yahoo.com" "romance beta reader"',
        # "Email me" invitations
        '"email me" "romance reader" "gmail.com"',
        '"contact me" "romance books" "gmail.com"',
        '"email me" "romance book club" "gmail.com"',
        '"reach me" "romance reader" "gmail.com"',
        '"email me at" "romance" "gmail.com"',
        '"email me" "romance reader" "yahoo.com"',
        # Organized reader activities
        '"gmail.com" "romance reading challenge"',
        '"gmail.com" "romance buddy read"',
        '"gmail.com" "romance book club" "contact"',
        '"gmail.com" "romance reading group" "contact"',
    ]

    # TIER 2: Personal blogs — highest email surface area
    tier2 = [
        '"gmail.com" "romance reader" site:blogspot.com',
        '"gmail.com" "romance book club" site:blogspot.com',
        '"gmail.com" "romance book review" site:blogspot.com',
        '"gmail.com" "romance book review blog" site:blogspot.com',
        '"gmail.com" "romance book lover" site:blogspot.com',
        '"@gmail.com" "romance reader" site:blogspot.com',
        '"@gmail.com" "romance book club" site:blogspot.com',
        '"@yahoo.com" "romance reader" site:blogspot.com',
        '"gmail.com" "romance reader" site:wordpress.com',
        '"gmail.com" "romance book club" site:wordpress.com',
        '"gmail.com" "romance book review blog" site:wordpress.com',
        '"@gmail.com" "romance readers" site:wordpress.com',
        '"yahoo.com" "romance reader" site:wordpress.com',
        '"yahoo.com" "romance book club" site:blogspot.com',
        '"hotmail.com" "romance reader" site:blogspot.com',
        '"outlook.com" "romance reader" site:blogspot.com',
    ]

    # TIER 3: Country TLD — skips retailer/publisher domains
    tier3 = [
        '"gmail.com" "romance readers" site:co.uk',
        '"hotmail.co.uk" "romance readers"',
        '"hotmail.co.uk" "romance book club"',
        '"@gmail.com" "romance readers" site:co.uk',
        '"gmail.com" "romance readers" site:com.ng',
        '"gmail.com" "romance book club" site:com.ng',
        '"@gmail.com" "romance readers" site:com.ng',
        '"gmail.com" "romance readers" site:co.za',
        '"gmail.com" "romance book club" site:co.za',
        '"gmail.com" "romance readers" site:co.ke',
        '"gmail.com" "romance book club" site:co.ke',
        '"gmail.com" "romance readers" site:com.gh',
        '"gmail.com" "romance readers" site:com.au',
        '"gmail.com" "romance book club" site:com.au',
        '"gmail.com" "romance readers" site:co.nz',
        '"gmail.com" "romance readers" site:ca',
        '"gmail.com" "romance book club" site:ca',
        '"gmail.com" "romance readers" site:ie',
        '"gmail.com" "romance readers" site:ph',
    ]

    # TIER 4: Subgenre-specific (high hit rate — very targeted)
    subgenres = [
        'dark romance', 'spicy romance', 'contemporary romance',
        'paranormal romance', 'historical romance', 'steamy romance',
        'mafia romance', 'billionaire romance', 'sports romance',
        'reverse harem romance',
    ]
    tier4 = []
    for sub in subgenres:
        tier4.append('"gmail.com" "' + sub + ' beta reader"')
        tier4.append('"gmail.com" "' + sub + ' arc reader"')
        tier4.append('"gmail.com" "' + sub + '" "book swap"')
        tier4.append('"gmail.com" "' + sub + ' readers" site:blogspot.com')
        tier4.append('"gmail.com" "' + sub + ' readers" site:wordpress.com')
        tier4.append('"@gmail.com" "' + sub + ' readers"')
        tier4.append('"email me" "' + sub + '" "gmail.com"')

    # TIER 5: Extended platforms (snippet extraction from communities)
    tier5 = [
        '"gmail.com" "romance" site:bookcrossing.com',
        '"yahoo.com" "romance" site:bookcrossing.com',
        '"gmail.com" "romance readers" site:librarything.com',
        '"gmail.com" "romance book club" site:librarything.com',
        '"gmail.com" "romance reader" site:tumblr.com',
        '"gmail.com" "romance book review" site:tumblr.com',
        '"@gmail.com" "romance reader" site:tumblr.com',
        '"gmail.com" "romance" "book swap" site:reddit.com',
        '"gmail.com" "romance arc" site:reddit.com',
        '"@gmail.com" "romance" site:reddit.com',
        '"gmail.com" "romance readers" site:goodreads.com',
        '"@gmail.com" "romance book club" site:goodreads.com',
        '"gmail.com" "romance reading group" site:goodreads.com',
        '"gmail.com" "contact for arcs" romance',
        '"gmail.com" "email for arcs" romance',
        '"gmail.com" "contact to join" "romance book club"',
        '"gmail.com" "email to join" "romance readers"',
        '"yahoo.com" "contact for arcs" romance',
        '"gmail.com" "romance newsletter" "subscribe"',
        '"@gmail.com" "romance newsletter"',
    ]

    # TIER 6: Country keyword matrix — broad sweep
    tier6 = []
    providers = ['gmail.com', 'yahoo.com', 'hotmail.com']
    reader_terms = ['"romance readers"', '"romance book club"', '"romance reader"']
    countries = [
        'Nigeria', '"South Africa"', 'Kenya', 'Ghana',
        'Australia', 'Canada', 'Ireland', '"New Zealand"',
        'Jamaica', 'Philippines', 'India', 'Uganda',
    ]
    for p in providers:
        for t in reader_terms:
            for c in countries:
                tier6.append('"' + p + '" ' + t + ' ' + c)

    # TIER 7 (adaptive): injected when yield drops 50%+ (expansion level 2+)
    tier7 = []
    if globals().get('DORK_EXTRA_PLATFORMS'):
        extra_platforms = [
            'site:wattpad.com', 'site:royalroad.com',
            'site:tumblr.com', 'site:deviantart.com',
        ]
        for site in extra_platforms:
            tier7.append('"gmail.com" "romance reader" ' + site)
            tier7.append('"gmail.com" "romance book club" ' + site)
            tier7.append('"@gmail.com" "romance" ' + site)
            tier7.append('"yahoo.com" "romance reader" ' + site)
        print("  DORK TIER 7 active: " + str(len(tier7)) + " extra-platform queries added")

    # Ordered dedup: tier1 first = highest yield always runs in earliest batch
    ordered = tier1 + tier2 + tier3 + tier4 + tier5 + tier6 + tier7
    seen = set()
    deduped = []
    for q in ordered:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def dork_search(batch_dork_queries):
    """
    Search DDG with email dork queries.
    Extracts emails from snippets directly — no page visit needed.
    Falls back to visiting the page only when snippet has no email.
    """
    # HARD BLOCK: no proxy at startup OR pool depleted mid-run → never hit DDG bare
    if SKIP_DDG_NO_PROXY or PROXY_DEPLETED:
        print("  Dork engine SKIPPED — proxy pool empty (GitHub IP protected)")
        return [], []
    print("\n--- Email Dork Engine running ---")
    print("  Dork queries this batch: " + str(len(batch_dork_queries)))

    direct_emails = []
    fallback_urls = []
    seen_emails = set()
    seen_urls = set()

    # Map query content to best DDG region
    DORK_REGION_MAP = {
        'site:co.uk': 'uk-en',   'hotmail.co.uk': 'uk-en',
        'site:com.au': 'au-en',  'Australia': 'au-en',
        'site:co.nz': 'nz-en',   'New Zealand': 'nz-en',
        'site:ca': 'ca-en',      'Canada': 'ca-en',
        'site:ie': 'ie-en',      'Ireland': 'ie-en',
        'site:com.ng': 'wt-wt',  'Nigeria': 'wt-wt',
        'site:co.za': 'wt-wt',   'South Africa': 'wt-wt',
        'site:co.ke': 'wt-wt',   'Kenya': 'wt-wt',
        'site:com.gh': 'wt-wt',  'Ghana': 'wt-wt',
        'site:ph': 'wt-wt',      'Philippines': 'wt-wt',
        'India': 'wt-wt',        'Jamaica': 'wt-wt',
        'Uganda': 'wt-wt',
    }
    _dork_region_cycle = ['us-en', 'uk-en', 'au-en', 'ca-en', 'wt-wt']
    _dork_ridx = [0]

    def pick_dork_region(q):
        for key, reg in DORK_REGION_MAP.items():
            if key in q:
                return reg
        r = _dork_region_cycle[_dork_ridx[0] % len(_dork_region_cycle)]
        _dork_ridx[0] += 1
        return r

    # Secondary regions: cross-region sweep doubles snippet coverage
    SECONDARY_REGION = {
        'us-en': 'uk-en', 'uk-en': 'us-en', 'au-en': 'us-en',
        'ca-en': 'us-en', 'ie-en': 'uk-en', 'nz-en': 'au-en',
        'wt-wt': 'us-en',
    }

    def run_dork_query(query, region):
        """Run one dork query, extract emails from snippets, return (emails, urls)."""
        emails_found = []
        urls_found = []
        try:
            proxy = get_next_proxy()
            with DDGS(proxy=proxy) as ddgs:
                results = list(ddgs.text(query, max_results=30, region=region))
            for r in results:
                url = r.get('href', '')
                snippet = r.get('body', '') + ' ' + r.get('title', '')
                for e in find_emails(snippet):
                    if e not in seen_emails:
                        seen_emails.add(e)
                        emails_found.append(e)
                        print("  DORK HIT: " + e + " (from snippet, " + region + ")")
                if url and url not in seen_urls and is_reader_website(url) and not find_emails(snippet):
                    if len(urls_found) < 5:  # max 5 fallback URLs per dork query
                        seen_urls.add(url)
                        urls_found.append(url)
            time.sleep(random.uniform(1, 2))
        except Exception as e:
            err = str(e)[:60]
            print("  Dork error (" + region + "): " + err)
            _evict_proxy(proxy)   # pull dead proxy from pool if it caused the DDG failure
            time.sleep(random.uniform(2, 3))
        return emails_found, urls_found

    for idx, query in enumerate(batch_dork_queries):
        # Primary region (geo-matched)
        primary = pick_dork_region(query)
        em, ur = run_dork_query(query, primary)
        direct_emails.extend(em)
        fallback_urls.extend(ur)

        # Secondary region (cross-region for higher coverage)
        secondary = SECONDARY_REGION.get(primary, 'us-en')
        if secondary != primary:
            em2, ur2 = run_dork_query(query, secondary)
            direct_emails.extend(em2)
            fallback_urls.extend(ur2)

        if (idx + 1) % 10 == 0:
            print("  Dork progress: " + str(idx + 1) + "/" + str(len(batch_dork_queries)) + " queries, " + str(len(direct_emails)) + " emails found")

    print("  Dork direct emails   : " + str(len(direct_emails)))
    print("  Dork fallback URLs   : " + str(len(fallback_urls)))
    return direct_emails, fallback_urls


# ============================================
# BLOG DIRECTORY SCRAPING
# ============================================

def scrape_blog_directories():
    print("\n--- Scraping blog directories ---")
    found_urls = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}

    skip_domains = [
        'feedspot.com', 'alltop.com', 'google.com', 'facebook.com',
        'twitter.com', 'instagram.com', 'youtube.com', 'pinterest.com',
        'amazon.com', 'goodreads.com', 'linkedin.com', 'reddit.com',
        'tiktok.com', 'tumblr.com',
    ]

    for directory_url in BLOG_DIRECTORIES:
        try:
            proxy = get_next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            response = requests.get(directory_url, headers=headers, proxies=proxies, timeout=6)
            soup = BeautifulSoup(response.text, 'html.parser')

            for tag in soup.find_all('a', href=True):
                href = tag['href']
                if not href.startswith('http'):
                    continue
                skip = any(d in href for d in skip_domains)
                if skip:
                    continue
                if href not in seen:
                    seen.add(href)
                    found_urls.append(href)

            print("  " + directory_url[:55] + " -> " + str(len(found_urls)) + " URLs")
            time.sleep(random.uniform(2, 3))
        except Exception as e:
            print("  Directory error: " + str(e)[:50])

    print("  Blog directories total: " + str(len(found_urls)) + " unique URLs")
    return found_urls

# ============================================
# URL FILTER
# ============================================

# Personal blog domains — only source type that reliably has exposed reader emails
ALLOWED_DOMAINS = [
    'blogspot.com', 'wordpress.com', 'wixsite.com', 'weebly.com',
    'tumblr.com', 'squarespace.com', 'ghost.io', 'substack.com',
    'medium.com', 'typepad.com', 'blogger.com',
]

# Hard block — never visit regardless
BLOCKED_DOMAINS = [
    # Publishers / retailers
    'amazon.com', 'barnesandnoble.com', 'harlequin.com', 'bookshop.org',
    'penguinrandomhouse.com', 'simonandschuster.com', 'macmillan.com',
    'targetbooks', 'walmart.com', 'ebay.com', 'etsy.com',
    'nextchapterbooksellers', 'thirdplacebooks', 'powells.com',
    'indiebound.org', 'booksamillion.com', 'chapters.indigo.ca',
    # Commercial book platforms
    'goodreads.com', 'bookbub.com', 'overdrive.com', 'libby.com',
    'scribd.com', 'wattpad.com', 'royalroad.com', 'webnovel.com',
    'netgalley.com', 'edelweiss', 'library',
    # Commercial club/event platforms
    'meetup.com', 'eventbrite.com', 'bookclubs.com', 'bookclubz.com',
    'literati.com', 'reese', 'swell', 'libro.fm',
    # Social media
    'facebook.com', 'instagram.com', 'twitter.com', 'tiktok.com',
    'youtube.com', 'pinterest.com', 'linkedin.com', 'snapchat.com',
    'reddit.com', 'discord.com', 'telegram.org',
    # News / media
    'forbes.com', 'buzzfeed.com', 'huffpost.com', 'theguardian.com',
    'nytimes.com', 'washingtonpost.com', 'bbc.com', 'cnn.com',
    'publishersweekly', 'writersdigest', 'literaryagency',
    'nielsen.com', 'statista.com',
    # Education / college institutional sites (NOT student blogs)
    'nces.ed.gov', 'commonapp.org', 'usnews.com', 'collegeboard.org',
    'collegenavigator', 'cappex.com', 'petersons.com',
    # Job / career sites
    'indeed.com', 'glassdoor.com', 'care.com', 'sittercity.com',
    # Reference / wiki
    'wikipedia.org', 'wikihow.com', 'britannica.com',
    # URL patterns
    '/images/', '/reel/', '/video/', '/watch?', '/tag/', '/category/',
    '/page/', '/search?', '/topics/', '/lists/',
]

def is_reader_website(url):
    """
    ALLOWLIST-first: only visit personal blogs and small personal sites.
    Everything else (commercial platforms, social media, publishers) blocked.
    This is why 514 visits returned 0 emails — wrong site types were visited.
    """
    url_lower = url.lower()

    # Hard block first
    for blocked in BLOCKED_DOMAINS:
        if blocked in url_lower:
            return False

    # Allowlist: personal blog platforms always pass
    for allowed in ALLOWED_DOMAINS:
        if allowed in url_lower:
            return True

    # For unknown domains: allow small personal sites
    # Block if URL looks like a commercial directory or list page
    suspicious_patterns = [
        '/join-a-book-club', '/best-book-clubs', '/book-club-picks',
        '/find-a-book-club', '/topics/', '/lists/', '/collections/',
        '/radical-romance', '/women-reading',
    ]
    for pattern in suspicious_patterns:
        if pattern in url_lower:
            return False

    # Unknown domain: allow small personal sites (hard block list above catches junk)
    return True

# ============================================
# PAGE SCRAPING
# ============================================

def _evict_proxy(proxy):
    """Thread-safe removal of a dead proxy. Masks URL in logs. Sets PROXY_DEPLETED if pool empty."""
    global PROXY_DEPLETED
    if not proxy:
        return
    with _PROXY_LOCK:
        if proxy in PROXY_LIST:
            PROXY_LIST.remove(proxy)
            masked = proxy.split('@')[-1][:20] if '@' in proxy else proxy[:20]
            remaining = len(PROXY_LIST)
            print("  PROXY EVICTED (" + str(remaining) + " remaining): ..." + masked)
            if remaining == 0:
                PROXY_DEPLETED = True
                print("  WARNING: All proxies exhausted mid-run — DDG calls halted to protect GitHub IP")

def scrape_page(url, headers, proxies, proxy_str=None):
    emails = []
    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        emails += find_emails(soup.get_text())
        for tag in soup.select('a[href^="mailto:"]'):
            href = tag.get('href', '')
            email = href.replace('mailto:', '').split('?')[0].strip()
            if '@' in email:
                emails.append(email)
    except Exception as e:
        err = str(e)[:60]
        if 'ProxyError' in err or '402' in err or '407' in err or 'Connection' in err:
            print("  PROXY ERR: " + err[:40])
            _evict_proxy(proxy_str)   # pull dead proxy out immediately
    return emails

def visit_website(url):
    headers = {'User-Agent': get_random_user_agent()}
    proxy = get_next_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    emails = scrape_page(url, headers, proxies, proxy_str=proxy)
    # Only visit /contact if main page had no emails — saves ~50% of HTTP calls
    if not emails:
        base = url.rstrip('/')
        emails += scrape_page(base + '/contact', headers, proxies, proxy_str=proxy)
    return list(set(emails))

# ============================================
# DAILY RUN CHECK (local only)
# ============================================

def already_ran_today():
    if not os.path.exists(TRACKER_FILE):
        return False
    try:
        with open(TRACKER_FILE, 'r') as f:
            data = json.load(f)
        return data.get('last_run_date', '') == datetime.now().strftime('%Y-%m-%d')
    except Exception:
        return False

def save_run_date():
    try:
        with open(TRACKER_FILE, 'w') as f:
            json.dump({'last_run_date': datetime.now().strftime('%Y-%m-%d')}, f)
    except Exception:
        pass

# ============================================
# EMAIL QUALITY REPORT
# ============================================

def analyze_emails(email_list):
    reader_indicators = [
        'book', 'read', 'romance', 'love', 'novel', 'story',
        'fiction', 'booktok', 'bibliophile', 'booklover', 'bookaddict'
    ]
    reader_count = sum(
        1 for e in email_list
        if any(ind in e.lower() for ind in reader_indicators)
    )
    total = len(email_list)
    if total > 0:
        pct = round((reader_count / total) * 100, 1)
        print("\n" + "=" * 60)
        print("EMAIL QUALITY REPORT:")
        print("  Total emails    : " + str(total))
        print("  Reader emails   : " + str(reader_count) + " (" + str(pct) + "%)")
        rating = "Excellent!" if pct > 70 else ("Good - improving" if pct > 50 else "Needs better keywords")
        print("  Rating          : " + rating)
        print("=" * 60)

# ============================================
# MAIN SCRAPER
# ============================================

def daily_scrape():
    print("=" * 60)
    print("ROMANCE READER EMAIL ROBOT - SELF-SUSTAINING ENGINE")
    print("Date: " + datetime.now().strftime('%B %d, %Y at %I:%M %p'))
    print("=" * 60)

    # ── Init proxy list FIRST so diagnostics shows correct count ──
    _init_proxy_list()
    print_startup_diagnostics()

    # ── Adaptive engine: MUST run before get_daily_keywords so expansion affects count ──
    expansion_level = get_expansion_level()
    if expansion_level > 0:
        apply_expansion(expansion_level)

    # --- Get today's keyword set (date-seeded rotation) ---
    all_keywords = get_daily_keywords()

    # --- Batch slice ---
    if IS_GITHUB_ACTIONS and BATCH > 0:
        batch_size = len(all_keywords) // 6
        start = (BATCH - 1) * batch_size
        end = start + batch_size if BATCH < 6 else len(all_keywords)
        all_keywords = all_keywords[start:end]
        print("Batch " + str(BATCH) + ": keywords " + str(start + 1) + "-" + str(end))

    # --- Sleep config ---
    INTER_URL_SLEEP = (0.5, 1.0) if IS_GITHUB_ACTIONS else (3, 6)
    KEYWORD_SLEEP   = (1, 2)     if IS_GITHUB_ACTIONS else (12, 18)
    COOLDOWN_SLEEP  = (40, 60)

    all_emails = []
    total_websites = 0
    skipped_ttl = 0
    skipped_blocked = 0

    visited_urls = load_visited_urls()
    fresh_count = count_fresh_urls(visited_urls)
    print("URL cache       : " + str(len(visited_urls)) + " tracked (" + str(fresh_count) + " expired and eligible for revisit)")
    print("Keywords        : " + str(len(all_keywords)) + " active this batch")
    print("Expansion level : " + str(expansion_level) + " (0=normal, 1=+kw, 2=+platforms, 3=+cache purge)")
    print("=" * 60)

    # --- Source 1: DDG multi-region + modifier + blog searches ---
    consecutive_failures = 0
    for idx, keyword in enumerate(all_keywords):
        print("\n[" + str(idx + 1) + "/" + str(len(all_keywords)) + "] " + keyword)

        urls = search_google(keyword, num_results=10, retry=1)
        total_websites += len(urls)
        if len(urls) == 0:
            consecutive_failures += 1
            if consecutive_failures >= 5:
                print("  PROXY DEAD: 5 consecutive 0-result keywords — skipping to dork engine")
                break
        else:
            consecutive_failures = 0

        visited_this_keyword = 0
        for url in urls:
            if visited_this_keyword >= 2:  # max 2 URL visits per keyword
                break
            if is_url_stale(visited_urls, url):
                skipped_ttl += 1
                continue
            if not is_reader_website(url):
                skipped_blocked += 1
                continue

            print("  Visiting: " + url[:70])
            emails = visit_website(url)
            mark_visited(visited_urls, url)
            visited_this_keyword += 1

            if emails:
                print("  Found " + str(len(emails)) + " email(s)!")
                all_emails.extend(emails)

            time.sleep(random.uniform(*INTER_URL_SLEEP))

        if (idx + 1) % 5 == 0:
            print("\n--- Progress: " + str(len(all_emails)) + " emails so far ---")
            save_visited_urls(visited_urls)

        # Mid-run checkpoint every 10 keywords — survives timeout kill
        if IS_GITHUB_ACTIONS and (idx + 1) % 10 == 0 and all_emails:
            save_master_emails(all_emails)
            try:
                import subprocess
                # Inject GITHUB_TOKEN into remote URL so push works from inside the script
                _gh_token = os.environ.get('GITHUB_TOKEN', '')
                _gh_repo  = os.environ.get('GITHUB_REPOSITORY', '')
                if _gh_token and _gh_repo:
                    _remote = 'https://x-access-token:' + _gh_token + '@github.com/' + _gh_repo + '.git'
                    subprocess.run(['git', 'remote', 'set-url', 'origin', _remote], capture_output=True)
                subprocess.run(['git', 'add', 'master_emails.txt', 'visited_urls.json'], capture_output=True)
                subprocess.run(['git', 'commit', '-m', 'bot: mid-run checkpoint [skip ci]'], capture_output=True)
                r = subprocess.run(['git', 'push', 'origin', 'main'], capture_output=True, text=True)
                if r.returncode == 0:
                    print("  Checkpoint committed (" + str(len(all_emails)) + " emails so far)")
                else:
                    print("  Checkpoint push failed: " + r.stderr[:60])
            except Exception as e:
                print("  Checkpoint skipped: " + str(e)[:40])

        if not IS_GITHUB_ACTIONS and (idx + 1) % 10 == 0:
            print("\n--- Cooling down... ---")
            time.sleep(random.uniform(*COOLDOWN_SLEEP))
        else:
            time.sleep(random.uniform(*KEYWORD_SLEEP))

    # --- Source 2: Email Dork Engine ---
    all_dork_queries = generate_dork_queries()
    # Split dork queries across 6 batches same as keywords
    if IS_GITHUB_ACTIONS and BATCH > 0:
        dork_batch_size = len(all_dork_queries) // 6
        dork_start = (BATCH - 1) * dork_batch_size
        dork_end = dork_start + dork_batch_size if BATCH < 6 else len(all_dork_queries)
        batch_dork_queries = all_dork_queries[dork_start:dork_end]
    else:
        batch_dork_queries = all_dork_queries  # full local run

    dork_emails, dork_fallback_urls = dork_search(batch_dork_queries)

    # Add direct dork emails immediately AND save — protects against any crash below
    all_emails.extend(dork_emails)
    if dork_emails:
        save_master_emails(all_emails)
        print("  Dork checkpoint: " + str(len(dork_emails)) + " emails saved immediately")

    # Visit fallback URLs (pages where snippet had no email) — hard cap to keep batch <3hrs
    MAX_FALLBACK = 75
    dork_fallback_urls = dork_fallback_urls[:MAX_FALLBACK]
    print("  Visiting " + str(len(dork_fallback_urls)) + " fallback URLs (capped at " + str(MAX_FALLBACK) + ")")
    for url in dork_fallback_urls:
        if is_url_stale(visited_urls, url):
            continue
        print("  [DORK FALLBACK] Visiting: " + url[:70])
        emails = visit_website(url)
        mark_visited(visited_urls, url)
        if emails:
            print("  Found " + str(len(emails)) + " email(s)!")
            all_emails.extend(emails)
        time.sleep(random.uniform(*INTER_URL_SLEEP))

    # --- Source 3: Blog directories (Batch 1 only, capped at 60 URLs) ---
    if not IS_GITHUB_ACTIONS or BATCH == 1:
        directory_urls = scrape_blog_directories()
        directory_urls = directory_urls[:60]  # cap — prevents Batch 1 running 3+ hrs
        total_websites += len(directory_urls)

        for url in directory_urls:
            if is_url_stale(visited_urls, url):
                skipped_ttl += 1
                continue
            if not is_reader_website(url):
                skipped_blocked += 1
                continue

            print("  [DIR] Visiting: " + url[:70])
            emails = visit_website(url)
            mark_visited(visited_urls, url)
            if emails:
                print("  Found " + str(len(emails)) + " email(s)!")
                all_emails.extend(emails)
            time.sleep(random.uniform(*INTER_URL_SLEEP))

<<<<<<< Updated upstream
    # --- Final save and    if len(all_emails) < 100:
        print("LOW: Check DIAGNOSTICS above — proxy or DDG issue")
    elif len(all_emails) < 400:
        print("BUILDING: Growing across batches toward 750")
    elif len(all_emails) < 750:
        print("GOOD: Heading toward 750+")
    else:
        print("TARGET REACHED: 750+ emails today!")
    print("=" * 60)

=======
    # --- Final save and report ---
    all_emails = clean_emails(all_emails)
    new_email_count = save_master_emails(all_emails)
    save_yield_tracker(yield_tracker)

    print("=" * 60)
    print("BATCH COMPLETE")
    print("  Emails found today      : " + str(len(all_emails)))
    print("  Added to master list    : " + str(new_email_count))
    print("  Websites visited        : " + str(total_websites - skipped_ttl - skipped_blocked))
    print("  Skipped (TTL - recent)  : " + str(skipped_ttl))
    print("  Skipped (blocked site)  : " + str(skipped_blocked))
    print("=" * 60)

    if len(all_emails) < 100:
        print("LOW: Check DIAGNOSTICS above — proxy or DDG issue")
    elif len(all_emails) < 400:
        print("BUILDING: Growing across batches toward 750")
    elif len(all_emails) < 750:
        print("GOOD: Heading toward 750+")
    else:
        print("TARGET REACHED: 750+ emails today!")
    print("=" * 60)

>>>>>>> Stashed changes

if __name__ == '__main__':
    daily_scrape()
