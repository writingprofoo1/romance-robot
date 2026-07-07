#!/usr/bin/env python3
"""
sender.py — Romance Robot Email Sender
Cohort-based 7-day drip via Brevo + Mailjet SMTP relays.
Runs on GitHub Actions 6x/day (at :30 past the hour).
All state persisted to CSV/JSON files auto-committed by workflow.
"""

import os
import csv
import json
import time
import random
import smtplib
import argparse
import logging
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================
# CONFIG
# ============================================

FROM_NAME         = "Romance Reads"
FROM_EMAIL        = "romancereads@lulllitcloud.com"
EMAILS_FILE       = "master_emails.txt"
CONTENT_FILE      = "content.csv"
TRACKING_FILE     = "email_tracking.csv"
RELAY_STATE_FILE  = "relay_state.json"
BOUNCED_FILE      = "bounced_emails.txt"
UNSUBSCRIBED_FILE = "unsubscribed_emails.txt"

STAGES = ["Hook", "Value", "Follow-up", "Close"]

# Stage → cohort day window (inclusive)
STAGE_DAYS = {
    "Hook":      (0, 1),
    "Value":     (2, 3),
    "Follow-up": (4, 5),
    "Close":     (6, 7),
}

# content.csv ID ranges per stage
STAGE_ID_RANGE = {
    "Hook":      (1,   250),
    "Value":     (251, 500),
    "Follow-up": (501, 750),
    "Close":     (751, 1000),
}

# tracking.csv stage → column name
STAGE_FIELD = {
    "Hook":      "hook_sent",
    "Value":     "value_sent",
    "Follow-up": "followup_sent",
    "Close":     "close_sent",
}

# Warm-up: (days_active_threshold, daily_cap)
WARMUP_SCHEDULE = [
    (7,    50),
    (14,  100),
    (21,  200),
    (28,  350),
    (9999, 500),
]

SMTP_RELAYS = [
    {
        "name":          "Brevo",
        "host":          "smtp-relay.brevo.com",
        "port":          587,
        "user":          os.environ.get("BREVO_USER", ""),
        "pass":          os.environ.get("BREVO_PASS", ""),
        "daily_limit":   300,
        "ssl":           False,
        "from_override": "romancereads@lulllitcloud.com",
    },
    {
        "name":          "Mailjet",
        "host":          "in-v3.mailjet.com",
        "port":          587,
        "user":          os.environ.get("MAILJET_USER", ""),
        "pass":          os.environ.get("MAILJET_PASS", ""),
        "daily_limit":   200,
        "ssl":           False,
        "from_override": "romancereads@lulllitcloud.com",
    },
]

# ============================================
# LOGGING
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sender")

# ============================================
# RELAY STATE — persistent daily send counts
# ============================================

def load_relay_state():
    """
    Load relay_state.json. Resets counters if date has changed.
    Preserves start_date across day resets (needed for warm-up math).
    """
    today = str(date.today())
    start_date = today  # default — overwritten below if file exists

    if os.path.exists(RELAY_STATE_FILE):
        try:
            with open(RELAY_STATE_FILE, "r") as f:
                state = json.load(f)
            # Preserve start_date regardless of day reset
            start_date = state.get("start_date", today)
            if state.get("date") == today:
                # Same day — return as-is, start_date already present
                state["start_date"] = start_date
                return state
        except Exception:
            pass

    # New day or corrupt file — reset counts, keep start_date
    return {
        "date":       today,
        "start_date": start_date,
        "relays":     {r["name"]: 0 for r in SMTP_RELAYS},
    }

def save_relay_state(state):
    with open(RELAY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_daily_cap(state):
    """Return today's send cap based on warm-up schedule."""
    start      = date.fromisoformat(state["start_date"])
    days_active = (date.today() - start).days + 1
    for threshold, cap in WARMUP_SCHEDULE:
        if days_active <= threshold:
            return cap
    return 500

def total_sent_today(state):
    return sum(state["relays"].values())

def pick_relay(state):
    """Return first relay with remaining daily capacity, or None."""
    for relay in SMTP_RELAYS:
        if state["relays"].get(relay["name"], 0) < relay["daily_limit"]:
            return relay
    return None

# ============================================
# SKIP LISTS — bounced + unsubscribed
# ============================================

def load_set(filepath):
    """Load a newline-delimited file into a lowercase set. Safe if missing."""
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r") as f:
        return {line.strip().lower() for line in f if line.strip()}

def append_to_file(filepath, value):
    with open(filepath, "a") as f:
        f.write(value.strip() + "\n")

# ============================================
# EMAIL TRACKING — cohort state per address
# ============================================

TRACKING_FIELDS = [
    "email", "cohort_start",
    "hook_sent", "value_sent", "followup_sent", "close_sent",
]

def load_tracking():
    """Load email_tracking.csv → dict keyed by lowercase email."""
    tracking = {}
    if not os.path.exists(TRACKING_FILE):
        return tracking
    with open(TRACKING_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tracking[row["email"].lower()] = row
    return tracking

def save_tracking(tracking):
    with open(TRACKING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        writer.writeheader()
        for row in tracking.values():
            writer.writerow(row)

def enroll(tracking, email, today):
    """Add email to cohort with today as start date if not already tracked."""
    if email not in tracking:
        tracking[email] = {
            "email":        email,
            "cohort_start": str(today),
            "hook_sent":    "0",
            "value_sent":   "0",
            "followup_sent":"0",
            "close_sent":   "0",
        }

def get_due_stage(row, today):
    """
    Return the stage due for this email today, or None.
    A stage is due when:
      - Its day window has arrived (days_since in [day_min, day_max])
      - It has not been sent yet
    """
    cohort_start = date.fromisoformat(row["cohort_start"])
    days_since   = (today - cohort_start).days

    for stage in STAGES:
        day_min, day_max = STAGE_DAYS[stage]
        field            = STAGE_FIELD[stage]
        if row.get(field) == "1":
            continue  # already sent
        if day_min <= days_since <= day_max:
            return stage
    return None  # nothing due (too early or sequence complete)

# ============================================
# CONTENT LIBRARY
# ============================================

def load_content():
    """Load content.csv → dict keyed by int ID."""
    content = {}
    if not os.path.exists(CONTENT_FILE):
        log.error("content.csv not found.")
        return content
    with open(CONTENT_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                content[int(row["ID"])] = row
            except (ValueError, KeyError):
                continue
    return content

def pick_content(content, stage):
    """
    Pick the least-used content row for the given stage.
    Selects randomly from the bottom 10% by Used Count (variety + fairness).
    """
    id_min, id_max = STAGE_ID_RANGE[stage]
    candidates = [
        row for cid, row in content.items()
        if id_min <= cid <= id_max
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: int(r.get("Used Count", 0)))
    pool_size = max(1, len(candidates) // 10)
    return random.choice(candidates[:pool_size])

def mark_content_used(content, row_id):
    """Increment Used Count in memory — batch-saved at end of run."""
    if row_id in content:
        content[row_id]["Used Count"] = str(int(content[row_id].get("Used Count", 0)) + 1)

def save_content(content):
    """Write content.csv once at end of run — not per email."""
    if not content:
        return
    fields = ["ID", "Category", "Subject", "Content", "Used Count"]
    with open(CONTENT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in sorted(content.values(), key=lambda r: int(r["ID"])):
            writer.writerow(row)

# ============================================
# SMTP SEND
# ============================================

def send_email(relay, to_email, subject, body, dry_run=False):
    """
    Send a single email via the given relay.
    Returns: True (success), False (soft fail), "bounce" (hard 5xx fail).
    """
    from_addr = relay.get("from_override") or FROM_EMAIL

    msg             = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = f"{FROM_NAME} <{from_addr}>"
    msg["To"]       = to_email
    msg.attach(MIMEText(body, "plain"))

    if dry_run:
        log.info(f"[DRY-RUN] {relay['name']} → {to_email} | {subject[:60]}")
        return True

    try:
        with smtplib.SMTP(relay["host"], relay["port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(relay["user"], relay["pass"])
            server.sendmail(from_addr, [to_email], msg.as_string())
        log.info(f"SENT [{relay['name']}] → {to_email} | {subject[:60]}")
        return True

    except smtplib.SMTPRecipientsRefused as e:
        code = list(e.recipients.values())[0][0]
        if 500 <= code <= 599:
            log.warning(f"HARD BOUNCE [{relay['name']}] {to_email} code={code}")
            return "bounce"
        log.warning(f"SOFT FAIL [{relay['name']}] {to_email} code={code}")
        return False

    except smtplib.SMTPResponseException as e:
        if 500 <= e.smtp_code <= 599:
            log.warning(f"HARD BOUNCE [{relay['name']}] {to_email} code={e.smtp_code}")
            return "bounce"
        log.warning(f"SMTP error [{relay['name']}] {to_email}: {e}")
        return False

    except Exception as e:
        log.warning(f"Send error [{relay['name']}] {to_email}: {e}")
        return False

# ============================================
# MAIN PIPELINE
# ============================================

def run(dry_run=False, limit=None):
    log.info("=== Romance Robot Sender starting ===")

    # Load all persistent state
    relay_state  = load_relay_state()
    bounced      = load_set(BOUNCED_FILE)
    unsubscribed = load_set(UNSUBSCRIBED_FILE)
    tracking     = load_tracking()
    content      = load_content()

    if not content:
        log.error("Aborting — content.csv missing or empty.")
        return

    today        = date.today()
    daily_cap    = get_daily_cap(relay_state)
    already_sent = total_sent_today(relay_state)
    budget       = daily_cap - already_sent

    log.info(
        f"Date={today} | start_date={relay_state['start_date']} | "
        f"warm-up cap={daily_cap} | sent today={already_sent} | budget={budget}"
    )

    if budget <= 0 and not dry_run:
        log.info("Daily cap already reached — exiting.")
        return

    # Load + validate email list
    if not os.path.exists(EMAILS_FILE):
        log.error(f"{EMAILS_FILE} not found — exiting.")
        return

    with open(EMAILS_FILE, "r") as f:
        raw = [line.strip().lower() for line in f if line.strip()]

    # Basic validation: must contain @ and a dot after @
    emails = [
        e for e in raw
        if "@" in e and "." in e.split("@", 1)[-1]
    ]
    log.info(f"Loaded {len(emails)} valid emails ({len(raw) - len(emails)} malformed skipped)")

    sent_count   = 0
    bounce_count = 0
    skip_count   = 0

    for email in emails:
        # Hard limit override (--limit flag or dry-run cap)
        if limit is not None and sent_count >= limit:
            log.info(f"--limit {limit} reached.")
            break

        # Daily budget check (skip in dry-run)
        if not dry_run and (budget - sent_count) <= 0:
            log.info("Daily budget exhausted — stopping.")
            break

        # Skip lists
        if email in bounced:
            skip_count += 1
            continue
        if email in unsubscribed:
            skip_count += 1
            continue

        # Enroll into cohort on first encounter
        enroll(tracking, email, today)

        # Determine which stage is due today
        stage = get_due_stage(tracking[email], today)
        if stage is None:
            continue  # not due or fully complete

        # Pick least-used content for this stage
        content_row = pick_content(content, stage)
        if not content_row:
            log.warning(f"No content available for stage={stage}, skipping {email}")
            continue

        row_id = int(content_row["ID"])

        # Pick relay with remaining capacity
        relay = pick_relay(relay_state)
        if relay is None and not dry_run:
            log.info("All relay limits reached — stopping.")
            break

        # Fallback: use first relay in dry-run (no actual send)
        active_relay = relay if relay else SMTP_RELAYS[0]

        # Send
        result = send_email(
            active_relay,
            email,
            content_row["Subject"],
            content_row["Content"],
            dry_run=dry_run,
        )

        if result == "bounce":
            append_to_file(BOUNCED_FILE, email)
            bounced.add(email)
            bounce_count += 1
            continue

        if result:
            # Mark stage sent in tracking
            tracking[email][STAGE_FIELD[stage]] = "1"
            # Increment relay counter (in-memory — saved at end)
            if not dry_run:
                relay_state["relays"][active_relay["name"]] = (
                    relay_state["relays"].get(active_relay["name"], 0) + 1
                )
            # Mark content used (in-memory — saved at end)
            mark_content_used(content, row_id)
            sent_count += 1
            # Throttle between sends (avoids burst SMTP rate limiting)
            time.sleep(random.uniform(1.5, 3.5))

    log.info(
        f"=== Run complete: sent={sent_count} | bounces={bounce_count} | skipped={skip_count} ==="
    )

    # Batch saves — once at end of run
    save_tracking(tracking)
    save_relay_state(relay_state)
    save_content(content)
    log.info("State saved.")

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Romance Robot Email Sender")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate sends — no SMTP, no relay counter increments",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max emails to process this run (overrides daily cap)",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
