#!/usr/bin/env python3
"""
unsubscribe_monitor.py — Romance Robot Inbox Monitor
Reads IMAP inbox at romancereads@lulllitcloud.com.
Classifies replies as STOP / positive / neutral and acts accordingly.
All libraries are Python built-in — no pip install required.

Phase 1: triggered via workflow_dispatch only.
Phase 2: cron added to .github/workflows/unsubscribe_monitor.yml after test passes.
"""

import os
import re
import csv
import ssl
import imaplib
import email
import email.header
import smtplib
import logging
import argparse
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================
# CONFIG
# ============================================

IMAP_HOST = "server353.web-hosting.com"
IMAP_PORT = 993

# ROMANCE_EMAIL defaults to the known address if secret not set
IMAP_USER = os.environ.get("ROMANCE_EMAIL", "romancereads@lulllitcloud.com")
IMAP_PASS = os.environ.get("ROMANCE_EMAIL_PASS", "")

FROM_NAME  = "Romance Reads"
FROM_EMAIL = "romancereads@lulllitcloud.com"

TRACKING_FILE     = "email_tracking.csv"
UNSUBSCRIBED_FILE = "unsubscribed_emails.txt"

TRACKING_FIELDS = [
    "email", "cohort_start",
    "hook_sent", "value_sent", "followup_sent", "close_sent",
]

# Brevo SMTP relay for auto-replies (same creds as sender.py)
BREVO = {
    "host": "smtp-relay.brevo.com",
    "port": 587,
    "user": os.environ.get("BREVO_USER", ""),
    "pass": os.environ.get("BREVO_PASS", ""),
}

# ============================================
# KEYWORD CLASSIFIERS
# ============================================

# STOP takes priority — checked first
STOP_KEYWORDS = [
    "stop",
    "unsubscribe",
    "remove me",
    "opt out",
    "opt-out",
    "take me off",
    "don't email",
    "do not email",
    "no more",
    "delete me",
]

POSITIVE_KEYWORDS = [
    "yes",
    "interested",
    "tell me more",
    "sign me up",
    "sounds good",
    "i'm in",
    "im in",
    "count me in",
    "love it",
    "yes please",
    "more info",
    "want to know more",
    "i want",
    "let me in",
    "how do i",
    "sign up",
    "subscribe",
    "where do i",
    "give me",
    "i'd like",
    "id like",
]

# ============================================
# AUTO-REPLY TEMPLATES
# ============================================

POSITIVE_SUBJECT = "Your 5 free chapters are ready"
POSITIVE_BODY = """\
Hi there, You said "yes" — so here's your escape.

I've opened the first 5 chapters of our collection just for you. No charge. No commitment. Just a few minutes of something that actually feels good.

Read your free chapters here: https://lulllitcloud.com/novels-public

Here's the thing — when you read a chapter that pulls you in, your brain releases dopamine and oxytocin. The same chemicals as falling in love. Your shoulders drop. Your mind calms. You feel human again.

After you've read the 5 chapters, if you want to keep going, you'll need to subscribe to the website. That's how you get access to the full library — more stories, more escapes, more of those feel-good moments whenever you need them.

No pressure. Just know the door is open.

Start reading here: https://lulllitcloud.com/novels-public

See you inside.

Romance Reads"""

NEUTRAL_SUBJECT = "Re: Romance Reads"
NEUTRAL_BODY = """\
Hi there,

Thanks for getting back to us.

We're Romance Reads — a curated collection of love stories for readers who believe a great story is worth their time. You received our email because you were identified as someone in the romance reading community.

If you'd like to explore what we have, you're welcome here: https://lulllitcloud.com/novels-public

If you'd rather not hear from us again, simply reply with STOP and we'll remove you immediately.

Romance Reads"""

# ============================================
# LOGGING
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ============================================
# CLASSIFICATION
# ============================================

def classify(body: str) -> str:
    """
    Returns: "stop", "positive", or "neutral".
    Uses word-boundary regex to avoid false matches (e.g. "stopping" ≠ "stop").
    STOP is checked first — takes priority over positive.
    """
    body_lower = body.lower()

    for kw in STOP_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, body_lower):
            return "stop"

    for kw in POSITIVE_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, body_lower):
            return "positive"

    return "neutral"

# ============================================
# EMAIL PARSING HELPERS
# ============================================

def extract_body(msg) -> str:
    """Return plain-text body from an email.message.Message object."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct   = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)

def decode_header_value(val: str) -> str:
    """Decode an RFC2047-encoded header value to a plain string."""
    if not val:
        return ""
    parts = email.header.decode_header(val)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)

def extract_sender_email(from_header: str) -> str:
    """
    Pull bare email address from a From header like 'Name <addr@domain.com>'.
    Falls back to the raw header if no angle-bracket address found.
    """
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1).strip().lower()
    return from_header.strip().lower()

# ============================================
# TRACKING — email_tracking.csv
# ============================================

def load_tracking() -> dict:
    tracking = {}
    if not os.path.exists(TRACKING_FILE):
        return tracking
    with open(TRACKING_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tracking[row["email"].lower()] = row
    return tracking

def save_tracking(tracking: dict):
    with open(TRACKING_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        writer.writeheader()
        for row in tracking.values():
            writer.writerow(row)

def mark_complete(tracking: dict, sender_email: str):
    """
    Set all 4 drip-stage flags to "1" for this address.
    If not already in tracking, enroll them fully complete —
    they're already a convert, no need to send the drip sequence.
    """
    if sender_email not in tracking:
        tracking[sender_email] = {
            "email":         sender_email,
            "cohort_start":  str(date.today()),
            "hook_sent":     "1",
            "value_sent":    "1",
            "followup_sent": "1",
            "close_sent":    "1",
        }
    else:
        row = tracking[sender_email]
        row["hook_sent"]     = "1"
        row["value_sent"]    = "1"
        row["followup_sent"] = "1"
        row["close_sent"]    = "1"

# ============================================
# UNSUBSCRIBED — unsubscribed_emails.txt
# ============================================

def append_unsub(email_addr: str):
    """Append one email address to the permanent opt-out list."""
    with open(UNSUBSCRIBED_FILE, "a") as f:
        f.write(email_addr.strip() + "\n")

# ============================================
# SMTP AUTO-REPLY (Brevo)
# ============================================

def send_reply(to_email: str, subject: str, body: str, dry_run: bool = False) -> bool:
    """
    Send an auto-reply via Brevo SMTP.
    Returns True on success, False on any failure.
    Never touches bounced_emails.txt — reply failures are transient.
    """
    if dry_run:
        log.info(f"[DRY-RUN] Would send '{subject}' → {to_email}")
        return True

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(BREVO["host"], BREVO["port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(BREVO["user"], BREVO["pass"])
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        log.info(f"REPLIED → {to_email} | {subject}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        log.error(f"AUTH FAILURE [Brevo] code={e.smtp_code} — check BREVO_USER / BREVO_PASS")
        return False

    except Exception as e:
        log.error(f"Reply send failed → {to_email}: {e}")
        return False

# ============================================
# MAIN PIPELINE
# ============================================

def run(dry_run: bool = False):
    log.info("=== Romance Robot Monitor starting ===")

    if not IMAP_PASS:
        log.error("ROMANCE_EMAIL_PASS not set — exiting.")
        return

    # ── Connect to IMAP ──────────────────────────────────────────────────
    try:
        ssl_ctx = ssl.create_default_context()
        mail    = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl_ctx)
        mail.login(IMAP_USER, IMAP_PASS)
        log.info(f"IMAP connected: {IMAP_USER} @ {IMAP_HOST}:{IMAP_PORT}")
    except Exception as e:
        log.error(f"IMAP connection failed: {e}")
        return

    try:
        mail.select("INBOX")

        # Fetch all UNSEEN message IDs
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            log.warning("IMAP UNSEEN search returned non-OK status — exiting.")
            return

        msg_ids = data[0].split()
        log.info(f"Unread messages found: {len(msg_ids)}")

        if not msg_ids:
            log.info("Inbox clear — nothing to process.")
            return

        tracking       = load_tracking()
        tracking_dirty = False

        processed      = 0
        stop_count     = 0
        positive_count = 0
        neutral_count  = 0

        for msg_id in msg_ids:
            # Fetch full RFC822 message
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                log.warning(f"Failed to fetch message id={msg_id} — skipping.")
                continue

            raw_email    = msg_data[0][1]
            msg          = email.message_from_bytes(raw_email)

            from_header  = decode_header_value(msg.get("From", ""))
            sender_email = extract_sender_email(from_header)
            subject      = decode_header_value(msg.get("Subject", "(no subject)"))
            body         = extract_body(msg)

            log.info(f"Processing: from={sender_email} | subject={subject[:60]}")

            # Safety guard — skip our own outbound messages landing in inbox
            if sender_email == FROM_EMAIL.lower():
                log.info("Skipping — message is from our own address.")
                if not dry_run:
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                continue

            # Classify the reply
            label = classify(body)
            log.info(f"→ Classified: {label.upper()}")

            # ── Act on classification ─────────────────────────────────────
            if label == "stop":
                if not dry_run:
                    append_unsub(sender_email)
                log.info(f"STOP: {sender_email} appended to {UNSUBSCRIBED_FILE}")
                stop_count += 1

            elif label == "positive":
                if not dry_run:
                    mark_complete(tracking, sender_email)
                    tracking_dirty = True
                send_reply(sender_email, POSITIVE_SUBJECT, POSITIVE_BODY, dry_run=dry_run)
                log.info(f"POSITIVE: {sender_email} — drip stopped, positive reply queued")
                positive_count += 1

            else:  # neutral
                send_reply(sender_email, NEUTRAL_SUBJECT, NEUTRAL_BODY, dry_run=dry_run)
                log.info(f"NEUTRAL: {sender_email} — holding reply queued")
                neutral_count += 1

            # Mark message as read so it won't be re-processed on next run
            if not dry_run:
                mail.store(msg_id, "+FLAGS", "\\Seen")

            processed += 1

        # ── Batch save tracking once at end (only if changed) ────────────
        if tracking_dirty:
            save_tracking(tracking)
            log.info("email_tracking.csv saved.")

        log.info(
            f"=== Run complete: processed={processed} | "
            f"stop={stop_count} | positive={positive_count} | neutral={neutral_count} ==="
        )

    except Exception as e:
        log.error(f"Unexpected monitor error: {e}")

    finally:
        try:
            mail.logout()
            log.info("IMAP disconnected.")
        except Exception:
            pass

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Romance Robot Inbox Monitor")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate — no SMTP replies, no file writes, no IMAP flag changes",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
