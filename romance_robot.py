# ROMANCE READER EMAIL ROBOT
# Finds romance readers from English-speaking countries worldwide!
# Targets: USA, UK, Canada, Australia, NZ, Nigeria, South Africa, Kenya, Ghana,
#          Zambia, Zimbabwe, Uganda, Tanzania, Botswana, Namibia, Ireland

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
import time
import re
import os
from datetime import datetime
import json
import random
from fake_useragent import UserAgent

# Detect GitHub Actions environment
IS_GITHUB_ACTIONS = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
BATCH = int(os.environ.get('BATCH', '0'))  # 0 = run all (local), 1-6 = batch (GitHub Actions)

# ============================================
# SETUP
# ============================================

TRACKER_FILE = "last_run.json"
VISITED_URLS_FILE = "visited_urls.json"
MASTER_EMAILS_FILE = "master_emails.txt"

# 4 DDG regions — full geographic coverage
# us-en: USA | uk-en: UK + Africa + Ireland | au-en: Australia + NZ + Asia | ca-en: Canada
DDG_REGIONS = ['us-en', 'uk-en', 'au-en', 'ca-en']

# Romance blog directories — scraped directly, bypasses search engine limits
BLOG_DIRECTORIES = [
    "https://blog.feedspot.com/romance_book_blogs/",
    "https://blog.feedspot.com/romance_book_review_blogs/",
    "https://alltop.com/romance",
    "https://www.thebookbloggerdirectory.com/",
    "https://www.bookbloggerlist.com/",
]

# Sequential proxy rotation — distributes load evenly across all 10 proxies
_proxy_index = [0]

def get_next_proxy():
    if not PROXY_LIST:
        return None
    proxy = PROXY_LIST[_proxy_index[0] % len(PROXY_LIST)]
    _proxy_index[0] += 1
    return proxy

# UserAgent created once at startup — avoids slow network call per URL
try:
    _ua = UserAgent()
    def get_random_user_agent():
        return _ua.random
except Exception:
    def get_random_user_agent():
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

def load_visited_urls():
    if not os.path.exists(VISITED_URLS_FILE):
        return set()
    try:
        with open(VISITED_URLS_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_visited_urls(visited):
    try:
        with open(VISITED_URLS_FILE, 'w') as f:
            json.dump(list(visited), f)
    except Exception:
        pass

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

# PROXY LIST - loaded from environment variable (set in GitHub Secrets)
# Strip trailing slashes — DDGS and requests both reject proxy URLs ending in /
_proxy_env = os.environ.get('PROXY_LIST', '')
PROXY_LIST = [p.strip().rstrip('/') for p in _proxy_env.split(',') if p.strip()]

# GITHUB TOKENS - loaded from environment variable (set in GitHub Secrets)
_token_env = os.environ.get('GITHUB_TOKENS', '')
GITHUB_TOKENS = [t.strip() for t in _token_env.split(',') if t.strip()]

def print_startup_diagnostics():
    print("=" * 60)
    print("DIAGNOSTICS:")
    print("  PROXY_LIST set  : " + ("YES - " + str(len(PROXY_LIST)) + " proxies" if PROXY_LIST else "NO - searches will use GitHub IP (DDG will block)"))
    if PROXY_LIST:
        # Print first proxy with password masked
        p = PROXY_LIST[0]
        parts = p.split('@')
        masked = parts[0].split(':')[0] + ':****@' + parts[1] if len(parts) == 2 else p[:20] + '...'
        print("  First proxy     : " + masked)
    print("  DDG regions     : " + str(DDG_REGIONS))
    print("  Batch           : " + str(BATCH))
    print("=" * 60)

# ============================================
# FIND EMAILS
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

    # Block only on the LOCAL PART (before @) being exactly these words
    blocked_local_exact = {
        'admin', 'webmaster', 'noreply', 'no-reply', 'donotreply',
        'support', 'help', 'info', 'contact', 'sales', 'marketing',
        'press', 'media', 'editor', 'editors', 'pr', 'ceo', 'cfo',
        'cto', 'founder', 'hello', 'team', 'staff', 'office'
    }

    # Block emails from obviously non-reader domains
    blocked_domains_exact = {
        'example.com', 'test.com', 'sentry.io', 'amazonaws.com',
        'cloudflare.com', 'wixsite.com', 'squarespace.com'
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
# MULTI-REGION DDG SEARCH
# ============================================

def ddg_search(query, region, num_results, retry):
    attempt = 0
    results = []
    seen = set()
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
            print("  DDG error (region=" + region + ", attempt=" + str(attempt) + "): " + str(e)[:80])
            time.sleep(random.uniform(2, 4))
    return results

def search_google(keyword, num_results=25, retry=3):
    print("  Searching: " + keyword)
    all_results = []
    seen = set()

    # Standard multi-region search
    for region in DDG_REGIONS:
        for url in ddg_search(keyword, region, num_results, retry):
            if url not in seen:
                seen.add(url)
                all_results.append(url)
        time.sleep(random.uniform(1, 2))

    # Blog-specific searches — personal blogs expose emails far more often
    for site in ['site:blogspot.com', 'site:wordpress.com']:
        blog_query = keyword + ' ' + site
        for url in ddg_search(blog_query, 'us-en', num_results, retry):
            if url not in seen:
                seen.add(url)
                all_results.append(url)
        time.sleep(random.uniform(1, 2))

    if len(all_results) == 0:
        print("  WARNING: 0 results — proxy may be blocked or PROXY_LIST empty")
    else:
        print("  Found " + str(len(all_results)) + " URLs (multi-region + blogs)")
    return all_results

# ============================================
# BLOG DIRECTORY SCRAPING (second source)
# ============================================

def scrape_blog_directories():
    print("\n--- Scraping blog directories for additional URLs ---")
    found_urls = []
    seen = set()
    headers = {'User-Agent': get_random_user_agent()}

    # Domains to skip — these are the directories themselves, not reader blogs
    skip_domains = [
        'feedspot.com', 'alltop.com', 'google.com', 'facebook.com',
        'twitter.com', 'instagram.com', 'youtube.com', 'pinterest.com',
        'amazon.com', 'goodreads.com', 'linkedin.com', 'reddit.com'
    ]

    for directory_url in BLOG_DIRECTORIES:
        try:
            proxy = get_next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            response = requests.get(directory_url, headers=headers, proxies=proxies, timeout=8)
            soup = BeautifulSoup(response.text, 'html.parser')

            for tag in soup.find_all('a', href=True):
                href = tag['href']
                if not href.startswith('http'):
                    continue
                skip = False
                for domain in skip_domains:
                    if domain in href:
                        skip = True
                        break
                if skip:
                    continue
                if href not in seen:
                    seen.add(href)
                    found_urls.append(href)

            print("  " + directory_url[:60] + " -> " + str(len(found_urls)) + " URLs so far")
            time.sleep(random.uniform(2, 3))

        except Exception as e:
            print("  Directory error: " + str(e)[:60])

    print("  Blog directories total: " + str(len(found_urls)) + " unique URLs")
    return found_urls

# ============================================
# VISIT WEBSITES
# ============================================

def scrape_page(url, headers, proxies):
    emails = []
    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=8)
        soup = BeautifulSoup(response.text, 'html.parser')

        emails += find_emails(soup.get_text())

        for tag in soup.select('a[href^="mailto:"]'):
            href = tag.get('href', '')
            email = href.replace('mailto:', '').split('?')[0].strip()
            if '@' in email:
                emails.append(email)

    except Exception:
        pass
    return emails

def visit_website(url):
    try:
        headers = {'User-Agent': get_random_user_agent()}
        proxy = get_next_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # Main page
        emails = scrape_page(url, headers, proxies)

        # /contact page — highest email yield of all sub-pages
        base = url.rstrip('/')
        time.sleep(0.5)
        emails += scrape_page(base + '/contact', headers, proxies)

        return list(set(emails))
    except Exception:
        return []

# ============================================
# READER FILTERS
# ============================================

def is_reader_website(url):
    url_lower = url.lower()
    # Block large platforms and non-personal sites — they never expose reader emails
    blocked_sites = [
        # Retailers & publishers
        'amazon.com', 'barnesandnoble.com', 'harlequin.com',
        'penguinrandomhouse.com', 'simonandschuster.com',
        'publishersweekly', 'writersdigest', 'literaryagency',
        # Social media — no scrapable emails
        'pinterest.com', 'instagram.com', 'facebook.com',
        'twitter.com', 'tiktok.com', 'youtube.com', 'reddit.com',
        'linkedin.com', 'tumblr.com', 'snapchat.com',
        # Research / news
        'nielsen.com', 'statista.com', 'forbes.com', 'buzzfeed.com',
        'huffpost.com', 'theguardian.com', 'nytimes.com',
        'washingtonpost.com', 'bbc.com', 'cnn.com',
        # Book platforms (author/retail, not readers)
        'goodreads.com', 'bookbub.com', 'overdrive.com',
        'librarything.com', 'storygraph.com',
        # Image / video
        '/images/', '/reel/', '/video/', '/watch?',
    ]
    for blocked in blocked_sites:
        if blocked in url_lower:
            return False
    return True

# ============================================
# RUN ONCE PER DAY (local only)
# ============================================

def already_ran_today():
    if not os.path.exists(TRACKER_FILE):
        return False
    try:
        with open(TRACKER_FILE, 'r') as f:
            data = json.load(f)
        last_run = data.get('last_run_date', '')
        today = datetime.now().strftime('%Y-%m-%d')
        return last_run == today
    except Exception:
        return False

def save_run_date():
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with open(TRACKER_FILE, 'w') as f:
            json.dump({'last_run_date': today}, f)
    except Exception:
        pass

# ============================================
# CHECK QUALITY
# ============================================

def analyze_emails(email_list):
    reader_indicators = [
        'book', 'read', 'romance', 'love', 'novel',
        'story', 'fiction', 'booktok', 'goodreads',
        'bibliophile', 'booklover', 'bookaddict'
    ]
    reader_count = 0
    for email in email_list:
        email_lower = email.lower()
        for indicator in reader_indicators:
            if indicator in email_lower:
                reader_count += 1
                break

    total = len(email_list)
    if total > 0:
        percent = (reader_count / total) * 100
        print("\n" + "=" * 60)
        print("EMAIL QUALITY REPORT:")
        print("  Total emails      : " + str(total))
        print("  Reader emails     : " + str(reader_count) + " (" + str(round(percent, 1)) + "%)")
        if percent > 70:
            print("  Rating: Excellent - mostly readers!")
        elif percent > 50:
            print("  Rating: Good - can improve")
        else:
            print("  Rating: Needs better keywords")
        print("=" * 60)

# ============================================
# MAIN SCRAPER
# ============================================

def daily_scrape():
    print("=" * 60)
    print("ROMANCE READER EMAIL ROBOT")
    print("Targeting: English-speaking countries worldwide!")
    print("Daily Target: 750-1000 READER emails")
    print("Date: " + datetime.now().strftime('%B %d, %Y at %I:%M %p'))
    print("=" * 60)
    print_startup_diagnostics()

    keywords = [
        # ---- USA ----
        "romance book club USA",
        "American romance readers",
        "romance books USA",
        "US romance book club",
        "romance readers America",
        "romance readers Texas",
        "romance readers California",
        "romance readers New York",
        "American South romance readers",
        "Midwest romance readers",
        # ---- UK ----
        "UK romance book club",
        "British romance readers",
        "romance books UK",
        "UK book lovers romance",
        "romance readers UK",
        "Scottish romance readers",
        "Welsh romance readers",
        "English romance readers",
        # ---- Canada ----
        "Canadian romance readers",
        "romance book club Canada",
        "romance books Canada",
        "Canada romance readers",
        # ---- Australia ----
        "Australian romance readers",
        "romance book club Australia",
        "romance books Australia",
        "Australia romance readers",
        # ---- New Zealand ----
        "New Zealand romance readers",
        "romance book club NZ",
        "NZ romance readers",
        # ---- Nigeria ----
        "romance book club Nigeria",
        "Nigerian romance readers",
        "romance books Nigeria",
        "Nigerian book lovers",
        "romance readers Nigeria",
        "Nigerian book blog",
        # ---- South Africa ----
        "South African romance readers",
        "romance book club South Africa",
        "romance books SA",
        "book club South Africa",
        "romance readers South Africa",
        # ---- Kenya ----
        "Kenyan romance readers",
        "romance book club Kenya",
        "romance books Kenya",
        # ---- Ghana ----
        "Ghanaian romance readers",
        "romance book club Ghana",
        "romance books Ghana",
        "Ghanaian book lovers",
        # ---- Zambia ----
        "Zambian romance readers",
        "romance book club Zambia",
        "romance books Zambia",
        # ---- Zimbabwe ----
        "Zimbabwe romance readers",
        "romance book club Zimbabwe",
        "romance books Zimbabwe",
        # ---- Uganda ----
        "Ugandan romance readers",
        "romance book club Uganda",
        "romance books Uganda",
        # ---- Tanzania ----
        "Tanzanian romance readers",
        "romance book club Tanzania",
        "romance books Tanzania",
        # ---- Botswana ----
        "Botswana romance readers",
        "romance book club Botswana",
        "romance books Botswana",
        # ---- Namibia ----
        "Namibian romance readers",
        "romance book club Namibia",
        "romance books Namibia",
        # ---- Ireland ----
        "Irish romance readers",
        "romance book club Ireland",
        "romance books Ireland",
        # ---- More Countries ----
        "Singapore romance readers",
        "Malaysia romance readers",
        "Philippines romance readers",
        "India romance readers",
        "Caribbean romance readers",
        "Jamaica romance readers",
        "Trinidad romance readers",
        "Pakistan romance readers",
        "African romance readers",
        "West African romance readers",
        "East African romance readers",
        "romance readers Europe",
        "romance readers Asia",
        "Southeast Asia romance readers",
        # ---- Reader Communities ----
        "goodreads romance reviews",
        "booktok romance recommendations",
        "romance book club members",
        "romance reader community",
        "romance book lovers group",
        "bookstagram romance readers",
        "romance book giveaway",
        "romance book subscription boxes readers",
        "goodreads romance readers",
        "booktok book recommendations romance",
        "romance readers tiktok",
        "romance books instagram",
        "romance book influencer",
        "romance book street team",
        "arc readers romance",
        "romance book buddy read",
        "romance reading buddy",
        "romance book pen pals",
        "romance book exchange",
        "romance book swap group",
        # ---- Reader Blogs ----
        "romance book review blog",
        "romance reader blog",
        "book lover blog romance",
        "romance novel recommendations blog",
        "romance book favorites blog",
        "best romance books review",
        "romance book review sites",
        "book review blog romance",
        "romance book blogger",
        "romance book review blogger",
        "book blogger romance genre",
        "romance genre book blog",
        "romance book review website",
        "romance book blog community",
        "romance book blog contact",
        "romance blogger contact",
        "romance book blog email",
        # ---- Self-Identified Readers ----
        "romance book addict",
        "romance book obsession",
        "bibliophile romance books",
        "booklover romance novels",
        "reading romance novels",
        "i love romance books",
        "romance book fan",
        "avid romance reader",
        # ---- Reader Forums ----
        "romance book discussion",
        "romance reader forum",
        "romance book club online",
        "romance book swap",
        "romance reading challenge",
        "romance book group",
        "online romance book club",
        "romance book talk",
        # ---- Trope-Specific Readers ----
        "enemies to lovers readers",
        "slow burn romance fans",
        "grumpy sunshine romance readers",
        "fake dating romance readers",
        "second chance romance fans",
        "historical romance readers",
        "contemporary romance readers",
        "spicy romance readers",
        "steamy romance readers",
        "dark romance readers",
        "forbidden romance readers",
        "age gap romance readers",
        "enemies to lovers book club",
        "slow burn romance book club",
        "dark romance book club 2025",
        "steamy romance book recommendations",
        # ---- Subgenre-Specific ----
        "paranormal romance readers",
        "regency romance readers",
        "military romance readers",
        "billionaire romance readers",
        "small town romance readers",
        "highland romance readers",
        "mafia romance readers",
        "reverse harem romance readers",
        "sports romance readers",
        "rockstar romance readers",
        "office romance readers",
        "romantic suspense readers",
        "shifter romance readers",
        "vampire romance readers",
        "fantasy romance readers",
        "cozy romance readers",
        "beach read romance fans",
        "omegaverse romance readers",
        "viking romance readers",
        "pirate romance readers",
        "cowboy romance readers",
        "werewolf romance readers",
        "fae romance readers",
        "dragon romance readers",
        "alien romance readers",
        "monster romance readers",
        # ---- Year-Based ----
        "romance readers 2025",
        "romance book club 2025",
        "romance book recommendations 2025",
        "best romance books 2025",
        "romance reading list 2025",
        "romance readers 2024",
        "romance book club 2024",
        "romance book recommendations 2024",
        "romance book haul 2025",
        "romance reading challenge 2025",
        # ---- Platform-Specific ----
        "kindle unlimited romance readers",
        "romance arc readers",
        "romance advance readers copy",
        "bookstagram romance community",
        "romance readers reddit",
        "romance readers facebook group",
        "romance book haul",
        "romance books TBR",
        "romance beta readers",
        "wattpad romance readers",
        "royal road romance readers",
        "webnovel romance readers",
        "romance audiobook listeners",
        "romance ebook readers",
        "bookbub romance readers",
        "litsy romance readers",
        "romance books audible",
        # ---- Author Fan Communities ----
        "colleen hoover readers",
        "nora roberts readers",
        "julia quinn readers",
        "lisa kleypas readers",
        "sarah maas readers",
        "bridgerton fans readers",
        "outlander readers fans",
        "diana gabaldon fans",
        "jennifer l armentrout readers",
        "penelope douglas readers",
        # ---- Newsletter/Subscription ----
        "romance newsletter subscribers",
        "romance book subscription box",
        "romance arc team",
        "romance bingo readers",
        "spicy book recommendations",
        "steamy book club members",
        "dark romance book club",
        "romance reader email list",
        "romance book club newsletter",
        "romance reading group newsletter",
        # ---- Format/Review Focused ----
        "romance book review email",
        "romance review site contact",
        "romance reader newsletter",
        "romance book blog list",
        "romance book ratings goodreads",
        "5 star romance books",
        "romance book unboxing",
        "romance book aesthetic",
        "romance book photography",
        "romance book collection",
        # ---- Occupation-Based ----
        "nurses who read romance",
        "teachers who read romance",
        "romance reading nurses",
        "romance reading teachers",
        "stay at home moms romance books",
        "romance books for women",
    ]

    # Batch slicing for GitHub Actions — 6 batches per day
    if IS_GITHUB_ACTIONS and BATCH > 0:
        batch_size = len(keywords) // 6
        start = (BATCH - 1) * batch_size
        end = start + batch_size if BATCH < 6 else len(keywords)
        keywords = keywords[start:end]
        print("GitHub Actions - Batch " + str(BATCH) + ": keywords " + str(start + 1) + " to " + str(end))

    # Sleep settings: tight in CI, relaxed locally
    INTER_URL_SLEEP = (0.5, 1.0) if IS_GITHUB_ACTIONS else (3, 6)
    KEYWORD_SLEEP   = (1, 2)     if IS_GITHUB_ACTIONS else (12, 18)
    COOLDOWN_SLEEP  = (40, 60)   # local only

    all_emails = []
    total_websites = 0
    skipped_websites = 0

    visited_urls = load_visited_urls()
    print("Tracking: " + str(len(visited_urls)) + " URLs already visited (will skip)")
    print("Keywords : " + str(len(keywords)) + " active")
    print("=" * 60)

    # --- Source 1: DDG multi-region search ---
    for idx, keyword in enumerate(keywords):
        print("\n[" + str(idx + 1) + "/" + str(len(keywords)) + "] " + keyword)

        urls = search_google(keyword, num_results=25, retry=3)
        total_websites += len(urls)

        for url in urls:
            if url in visited_urls:
                skipped_websites += 1
                continue
            if not is_reader_website(url):
                skipped_websites += 1
                continue

            print("  Visiting: " + url[:70])
            emails = visit_website(url)
            visited_urls.add(url)

            if emails:
                print("  Found " + str(len(emails)) + " email(s)!")
                all_emails.extend(emails)

            time.sleep(random.uniform(*INTER_URL_SLEEP))

        if (idx + 1) % 5 == 0:
            print("\n--- Progress: " + str(len(all_emails)) + " emails so far ---")
            save_visited_urls(visited_urls)

        if not IS_GITHUB_ACTIONS and (idx + 1) % 10 == 0:
            print("\n--- Cooling down... ---")
            time.sleep(random.uniform(*COOLDOWN_SLEEP))
        else:
            time.sleep(random.uniform(*KEYWORD_SLEEP))

    # --- Source 2: Blog directory scraping (Batch 1 only to avoid duplication) ---
    if not IS_GITHUB_ACTIONS or BATCH == 1:
        directory_urls = scrape_blog_directories()
        total_websites += len(directory_urls)

        for url in directory_urls:
            if url in visited_urls:
                skipped_websites += 1
                continue
            if not is_reader_website(url):
                skipped_websites += 1
                continue

            print("  [DIR] Visiting: " + url[:70])
            emails = visit_website(url)
            visited_urls.add(url)

            if emails:
                print("  Found " + str(len(emails)) + " email(s)!")
                all_emails.extend(emails)

            time.sleep(random.uniform(*INTER_URL_SLEEP))

    save_visited_urls(visited_urls)

    all_emails = clean_emails(all_emails)
    all_emails = list(set(all_emails))

    analyze_emails(all_emails)

    new_email_count = save_master_emails(all_emails)

    filename = "romance_readers_" + datetime.now().strftime('%Y%m%d') + "_batch" + str(BATCH) + ".txt"
    try:
        with open(filename, 'w') as f:
            f.write("# ROMANCE READER EMAILS - " + datetime.now().strftime('%B %d, %Y') + "\n")
            f.write("# Batch: " + str(BATCH) + "\n")
            f.write("# Today new emails: " + str(len(all_emails)) + "\n")
            f.write("# New additions to master list: " + str(new_email_count) + "\n")
            f.write("#" + "=" * 50 + "\n\n")
            for email in all_emails:
                f.write(email + '\n')
    except Exception as e:
        print("Warning: could not save output file: " + str(e))

    save_run_date()

    print("\n" + "=" * 60)
    print("FINAL STATISTICS:")
    print("  Reader emails found today : " + str(len(all_emails)))
    print("  New additions to master   : " + str(new_email_count))
    print("  Websites visited          : " + str(total_websites - skipped_websites))
    print("  Skipped (seen before)     : " + str(skipped_websites))
    print("  Saved to                  : " + filename)
    print("=" * 60)

    if len(all_emails) < 100:
        print("LOW: Check proxy connection and DDG logs above")
    elif len(all_emails) < 400:
        print("BUILDING: Accumulating across batches")
    elif len(all_emails) < 750:
        print("GOOD: Over 400 - heading toward 750+")
    else:
        print("TARGET REACHED! 750-1000 emails daily")
    print("=" * 60)

    return all_emails

# ============================================
# START
# ============================================

if __name__ == "__main__":
    if already_ran_today() and not IS_GITHUB_ACTIONS:
        print("=" * 60)
        print("YOU ALREADY RAN TODAY!")
        print("Date: " + datetime.now().strftime('%Y-%m-%d'))
        print("Come back tomorrow for fresh emails!")
        print("=" * 60)
    else:
        daily_scrape()

    if not IS_GITHUB_ACTIONS:
        input("\nPress ENTER to close...")
