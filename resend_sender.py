"""
resend_sender.py
------------------
Sends outreach emails via Resend (https://resend.com) to your verified
lead list, implementing the deliverability practices you'd want before
running any real cold-email campaign:

- Only sends to addresses marked "valid" by verify-emails (skip
  "invalid" entirely; "unknown" is opt-in via --include-unknown).
- Warm-up daily send cap, tracked across runs/days, so a new domain
  doesn't get flagged by sending too much too fast.
- Permanent suppression list — anything that hard-bounces or
  unsubscribes is added automatically and never emailed again, even
  across future uploads/campaigns.
- Sent-log dedup — never emails the same address twice, ever.
- Adds a proper List-Unsubscribe header (RFC 8058 one-click), which
  Gmail/Yahoo now effectively require for bulk senders.

SETUP:
1. Sign up at https://resend.com, verify a SENDING domain (ideally a
   dedicated subdomain like mail.yourcompany.com, not your primary
   domain — see the warm-up notes below for why).
2. Add SPF, DKIM, and DMARC records Resend gives you for that domain.
   Do this BEFORE sending anything — an unauthenticated domain gets
   flagged faster than a plain old domain would.
3. Add to your .env:

       RESEND_API_KEY=re_your_key_here
       FROM_NAME=Your Name
       FROM_EMAIL=you@mail.yourcompany.com
       REPLY_TO=you@yourcompany.com
       UNSUBSCRIBE_BASE_URL=https://yourcompany.com/unsubscribe

4. Edit email_template.txt (same template used by email_sender.py).

USAGE:
    # Always dry-run first:
    python resend_sender.py verified_emails.csv --dry-run

    # Real send, warming up a new domain — start small:
    python resend_sender.py verified_emails.csv --daily-limit 50

    # Later in the warm-up schedule, once reputation is established:
    python resend_sender.py verified_emails.csv --daily-limit 500

WARM-UP GUIDANCE (from standard deliverability practice):
    Week 1: 50-100/day       Week 3: 300-500/day
    Week 2: 150-250/day      Week 4+: scale to target volume
Re-run this script daily (e.g. via Windows Task Scheduler) with a
rising --daily-limit each week — don't blow past the cap in one run.
"""

import os
import csv
import json
import time
import argparse
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_NAME = os.environ.get("FROM_NAME", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
REPLY_TO = os.environ.get("REPLY_TO", FROM_EMAIL)
UNSUBSCRIBE_BASE_URL = os.environ.get("UNSUBSCRIBE_BASE_URL", "")

RESEND_URL = "https://api.resend.com/emails"

SENT_LOG_PATH = os.path.join(BASE_DIR, "sent_log.csv")
SUPPRESSION_PATH = os.path.join(BASE_DIR, "suppression.csv")
DAILY_STATE_PATH = os.path.join(BASE_DIR, "daily_send_state.json")


# ---------- persistence helpers ----------

def load_email_set(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["email"].strip().lower() for row in csv.DictReader(f) if row.get("email")}


def append_log(path, fieldnames, row):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def add_to_suppression(email, reason):
    append_log(SUPPRESSION_PATH, ["email", "reason", "added_at"], {
        "email": email, "reason": reason, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
    })


def load_daily_state():
    today = time.strftime("%Y-%m-%d")
    if os.path.exists(DAILY_STATE_PATH):
        with open(DAILY_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    return {"date": today, "sent_today": 0}


def save_daily_state(state):
    with open(DAILY_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------- template ----------

def load_template(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("SUBJECT:"):
        raise ValueError("Template must start with a line like: SUBJECT: your subject here")
    first_line, _, rest = content.partition("\n")
    subject = first_line[len("SUBJECT:"):].strip()
    body = rest.lstrip("\n")
    return subject, body


# ---------- sending ----------

def send_via_resend(to_email, subject, text_body, unsubscribe_url):
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to_email],
        "reply_to": REPLY_TO,
        "subject": subject,
        "text": text_body,
        "headers": {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    resp = requests.post(RESEND_URL, headers=headers, json=payload, timeout=15)
    return resp


def main():
    parser = argparse.ArgumentParser(description="Send outreach emails via Resend, warm-up aware")
    parser.add_argument("csv_in", help="Path to verified_emails.csv (from the Verify Emails page)")
    parser.add_argument("--template", default=os.path.join(BASE_DIR, "email_template.txt"))
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between sends (default 3)")
    parser.add_argument("--daily-limit", type=int, default=50, help="Max sends today, across all runs today (default 50)")
    parser.add_argument("--include-unknown", action="store_true", help="Also send to 'unknown' status addresses, not just 'valid'")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, sends nothing")
    args = parser.parse_args()

    if not args.dry_run and not (RESEND_API_KEY and FROM_EMAIL):
        print("ERROR: RESEND_API_KEY and FROM_EMAIL must be set in .env for a real send.")
        print("Use --dry-run to preview without sending.")
        return

    subject_template, body_template = load_template(args.template)
    already_sent = load_email_set(SENT_LOG_PATH)
    suppressed = load_email_set(SUPPRESSION_PATH)
    daily_state = load_daily_state()

    with open(args.csv_in, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    allowed_statuses = {"valid"} | ({"unknown"} if args.include_unknown else set())
    targets = [r for r in rows if (r.get("email") or "").strip() and (r.get("status") or "").lower() in allowed_statuses]
    print(f"{len(targets)} address(es) match status filter {sorted(allowed_statuses)} out of {len(rows)} in file.")

    remaining_today = max(0, args.daily_limit - daily_state["sent_today"])
    print(f"Daily cap: {args.daily_limit}, already sent today: {daily_state['sent_today']}, remaining: {remaining_today}")

    sent_count = 0
    for row in targets:
        if sent_count >= remaining_today:
            print(f"\nHit today's warm-up cap ({args.daily_limit}). Stopping — run again tomorrow, or raise --daily-limit once you're further into warm-up.")
            break

        email = row["email"].strip()
        name = row.get("name", "").strip() or "there"
        email_lower = email.lower()

        if email_lower in suppressed:
            print(f"  SKIP (suppressed — prior bounce/unsubscribe): {name} <{email}>")
            continue
        if email_lower in already_sent:
            print(f"  SKIP (already emailed): {name} <{email}>")
            continue

        subject = subject_template.format(business_name=name)
        body = body_template.format(business_name=name)
        unsubscribe_url = f"{UNSUBSCRIBE_BASE_URL}?email={email}" if UNSUBSCRIBE_BASE_URL else f"mailto:{REPLY_TO}?subject=unsubscribe"

        if args.dry_run:
            print(f"  [DRY RUN] Would send to {name} <{email}> — subject: {subject}")
            continue

        resp = send_via_resend(email, subject, body, unsubscribe_url)

        if resp.status_code in (200, 201):
            append_log(SENT_LOG_PATH, ["email", "business_name", "sent_at"], {
                "email": email, "business_name": name, "sent_at": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            print(f"  SENT to {name} <{email}>")
            sent_count += 1
            daily_state["sent_today"] += 1
            save_daily_state(daily_state)
            time.sleep(args.delay)
        elif resp.status_code in (429,):
            print(f"  RATE LIMITED by Resend — stopping this run early. {resp.text}")
            break
        else:
            print(f"  FAILED for {name} <{email}>: {resp.status_code} {resp.text}")

    if args.dry_run:
        print("\nDry run complete. No emails were sent.")
    else:
        print(f"\nDone. Sent {sent_count} email(s) this run. {daily_state['sent_today']}/{args.daily_limit} used today.")


if __name__ == "__main__":
    main()
