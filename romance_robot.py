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
import sys
from datetime import datetime
import json
import random
from fake_useragent import UserAgent

# Detect GitHub Actions environment
IS_GITHUB_ACTIONS = os.environ.get('GITHUB_ACTIONS', 'false').lower() == 'true'
BATCH = int(os.environ.get('BATCH', '0'))  # 0 = run all (local), 1-4 = batch (GitHub Actions)

# ============================================
# SETUP
# ============================================

TRACKER_FILE = "last_run.json"
VISITED_URLS_FILE = "visited_urls.json"
MASTER_EMAILS_FILE = "master_emails.txt"

# 7 DDG regions - each returns different results for the same keyword
DDG_REGIONS = ['us-en', 'uk-en', 'au-en', 'ca-en', 'za-en', 'ie-en', 'nz-en']

def load_visited_urls():
    if not os.path.exists(VISITED_URLS_FILE):
        return set()
    with open(VISITED_URLS_FILE, 'r') as f:
        return set(json.load(f))

def save_visited_urls(visited):
    with open(VISITED_URLS_FILE, 'w') as f:
        json.dump(list(visited), f)

def load_master_emails():
    if not os.path.exists(MASTER_EMAILS_FILE):
        return set()
    with open(MASTER_EMAILS_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def save_master_emails(new_emails):
    existing = load_master_emails()
    combined = existing | set(new_emails)
    with open(MASTER_EMAILS_FILE, 'w') as f:
        for email in sorted(combined):
            f.write(email + '\n')
    return len(combined) - len(existing)

# PROXY LIST - loaded from environment variable (set in GitHub Secrets)
_proxy_env = os.environ.get('PROXY_LIST', '')
PROXY_LIST = [p.strip() for p in _proxy_env.split(',') if p.strip()]

# GITHUB TOKENS - loaded from environment variable (set in GitHub Secrets)
_token_env = os.environ.get('GITHUB_TOKENS', '')
GITHUB_TOKENS = [t.strip() for t in _token_env.split(',') if t.strip()]

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

    blocked_domains = [
        'example.com', 'test.com', 'admin', 'webmaster',
        'info', 'noreply', 'support', 'sales', 'contact',
        'help', 'service', 'marketing', 'press', 'media',
        'pr', 'editor', 'founder', 'ceo', 'cfo', 'cto'
    ]

    blocked_keywords = [
        'author', 'writer', 'novelist', 'publisher', 'press',
        'agency', 'agent', 'literary', 'bookstore', 'shop',
        'store', 'market'
    ]

    clean_list = []
    for email in email_list:
        if '@' not in email:
            continue
        email_lower = email.lower()
        skip = False
        for blocked in blocked_domains:
            if blocked in email_lower:
                skip = True
                break
        if skip:
            continue
        for blocked in blocked_keywords:
            if blocked in email_lower:
                skip = True
                break
        if skip:
            continue
        clean_list.append(email)

    return clean_list

# ============================================
# MULTI-REGION SEARCH
# ============================================

def search_google(keyword, num_results=25, retry=3):
    print("  Searching: " + keyword)
    all_results = []
    seen = set()

    for region in DDG_REGIONS:
        attempt = 0
        while attempt < retry:
            try:
                with DDGS() as ddgs:
                    for r in ddgs.text(keyword, max_results=num_results, region=region):
                        url = r['href']
                        if url not in seen:
                            seen.add(url)
                            all_results.append(url)
                break
            except Exception as e:
                attempt += 1
                time.sleep(random.uniform(2, 4))
        time.sleep(random.uniform(1, 2))

    print("  Found " + str(len(all_results)) + " websites across " + str(len(DDG_REGIONS)) + " regions")
    return all_results

def get_random_user_agent():
    ua = UserAgent()
    return ua.random

# ============================================
# VISIT WEBSITES
# ============================================

def scrape_page(url, headers, proxies):
    emails = []
    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=15)
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
        proxy = random.choice(PROXY_LIST) if PROXY_LIST else None
        proxies = {"http": proxy, "https": proxy} if proxy else None

        emails = scrape_page(url, headers, proxies)

        base = url.rstrip('/')
        for path in ['/contact', '/about', '/contact-us', '/about-us']:
            time.sleep(1)
            emails += scrape_page(base + path, headers, proxies)

        return list(set(emails))
    except Exception:
        return []

# ============================================
# READER FILTERS
# ============================================

def is_reader_website(url):
    url_lower = url.lower()
    blocked_sites = [
        'amazon.com', 'barnesandnoble.com', 'harlequin.com',
        'penguinrandomhouse.com', 'simonandschuster.com',
        'literaryagency', 'publishersweekly', 'writersdigest'
    ]
    for blocked in blocked_sites:
        if blocked in url_lower:
            return False
    return True

# ============================================
# RUN ONCE PER DAY
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
    with open(TRACKER_FILE, 'w') as f:
        json.dump({'last_run_date': today}, f)

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
    print("Proxies : " + str(len(PROXY_LIST)) + " configured")
    print("Regions : " + str(len(DDG_REGIONS)) + " DDG regions active")
    print("=" * 60)

    keywords = [
        # USA
        "romance book club USA",
        "American romance readers",
        "romance books USA",
        "US romance book club",
        "romance readers America",
        # UK
        "UK romance book club",
        "British romance readers",
        "romance books UK",
        "UK book lovers romance",
        "romance readers UK",
        # Canada
        "Canadian romance readers",
        "romance book club Canada",
        "romance books Canada",
        "Canada romance readers",
        # Australia
        "Australian romance readers",
        "romance book club Australia",
        "romance books Australia",
        "Australia romance readers",
        # New Zealand
        "New Zealand romance readers",
        "romance book club NZ",
        "NZ romance readers",
        # Nigeria
        "romance book club Nigeria",
        "Nigerian romance readers",
        "romance books Nigeria",
        "Nigerian book lovers",
        "romance readers Nigeria",
        # South Africa
        "South African romance readers",
        "romance book club South Africa",
        "romance books SA",
        "book club South Africa",
        "romance readers South Africa",
        # Kenya
        "Kenyan romance readers",
        "romance book club Kenya",
        "romance books Kenya",
        # Ghana
        "Ghanaian romance readers",
        "romance book club Ghana",
        "romance books Ghana",
        # Zambia
        "Zambian romance readers",
        "romance book club Zambia",
        "romance books Zambia",
        # Zimbabwe
        "Zimbabwe romance readers",
        "romance book club Zimbabwe",
        "romance books Zimbabwe",
        # Uganda
        "Ugandan romance readers",
        "romance book club Uganda",
        "romance books Uganda",
        # Tanzania
        "Tanzanian romance readers",
        "romance book club Tanzania",
        "romance books Tanzania",
        # Botswana
        "Botswana romance readers",
        "romance book club Botswana",
        "romance books Botswana",
        # Namibia
        "Namibian romance readers",
        "romance book club Namibia",
        "romance books Namibia",
        # Ireland
        "Irish romance readers",
        "romance book club Ireland",
        "romance books Ireland",
        # New Countries
        "Singapore romance readers",
        "Malaysia romance readers",
        "Philippines romance readers",
        "India romance readers",
        "Caribbean romance readers",
        "Jamaica romance readers",
        "Trinidad romance readers",
        "Pakistan romance readers",
        # Reader Communities (Global)
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
        # Reader Blogs
        "romance book review blog",
        "romance reader blog",
        "book lover blog romance",
        "romance novel recommendations blog",
        "romance book favorites blog",
        "best romance books review",
        "romance book review sites",
        "book review blog romance",
        # Self-Identified Readers
        "romance book addict",
        "romance book obsession",
        "bibliophile romance books",
        "booklover romance novels",
        "reading romance novels",
        "i love romance books",
        "romance book fan",
        "avid romance reader",
        # Reader Forums
        "romance book discussion",
        "romance reader forum",
        "romance book club online",
        "romance book swap",
        "romance reading challenge",
        "romance book group",
        "online romance book club",
        "romance book talk",
        # Trope-Specific Readers
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
        # Subgenre-Specific (NEW)
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
        # Year-Based (NEW)
        "romance readers 2025",
        "romance book club 2025",
        "romance book recommendations 2025",
        "best romance books 2025",
        "romance reading list 2025",
        "romance readers 2024",
        "romance book club 2024",
        "romance book recommendations 2024",
        # Platform-Specific (NEW)
        "kindle unlimited romance readers",
        "romance arc readers",
        "romance advance readers copy",
        "bookstagram romance community",
        "romance readers reddit",
        "romance readers facebook group",
        "romance book haul",
        "romance books TBR",
        "romance beta readers",
        # Newsletter/Subscription (NEW)
        "romance newsletter subscribers",
        "romance book subscription box",
        "romance arc team",
        "romance reading challenge 2025",
        "romance bingo readers",
        "spicy book recommendations",
        "steamy book club members",
        "dark romance book club",
    ]

    # Batch slicing for GitHub Actions (splits keywords into 4 daily chunks)
    if IS_GITHUB_ACTIONS and BATCH > 0:
        batch_size = len(keywords) // 4
        start = (BATCH - 1) * batch_size
        end = start + batch_size if BATCH < 4 else len(keywords)
        keywords = keywords[start:end]
        print("GitHub Actions - Batch " + str(BATCH) + ": keywords " + str(start + 1) + " to " + str(end))

    # Sleep settings: tighter in CI, relaxed locally
    INTER_URL_SLEEP = (0.5, 1.5) if IS_GITHUB_ACTIONS else (3, 6)
    KEYWORD_SLEEP   = (3, 5)     if IS_GITHUB_ACTIONS else (12, 18)
    COOLDOWN_SLEEP  = (10, 15)   if IS_GITHUB_ACTIONS else (40, 60)

    all_emails = []
    total_websites = 0
    skipped_websites = 0

    visited_urls = load_visited_urls()
    print("Tracking: " + str(len(visited_urls)) + " URLs already visited (will skip)")
    print("Keywords : " + str(len(keywords)) + " active")
    print("=" * 60)

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
 