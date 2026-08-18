"""
email_verifier.py
-------------------
Checks whether an email address is likely to be real and able to
receive mail, WITHOUT actually sending anything to it. Three checks,
each one only run if the previous passes:

1. Syntax  — does it look like a valid address at all.
2. MX record — does the domain even have a mail server configured.
3. SMTP probe — connect to that mail server and ask "would you accept
   mail for this address?" (RCPT TO), then disconnect before actually
   sending a message. This is the same technique commercial email
   verification tools (NeverBounce, ZeroBounce, Hunter, etc.) use
   under the hood.

IMPORTANT CAVEATS — read before trusting the results:
- Many mail providers (Gmail, Outlook/Microsoft, Yahoo) refuse to
  confirm or deny individual mailboxes over SMTP specifically to
  block this kind of probing. Those will usually come back "unknown",
  not because anything is wrong with the address.
- "Catch-all" domains accept RCPT TO for ANY address at that domain
  (real or not) to avoid leaking which addresses exist. Those will
  show as "valid" even if the specific address is fake — the CSV
  flags this so you know to treat it with caution.
- Most residential ISPs and mobile networks BLOCK outbound port 25
  entirely (a very common global anti-spam measure). If that's the
  case here, every single check will silently time out and come back
  "unknown" regardless of the actual email. There's a quick built-in
  test for this — see check_port_25().
- None of this guarantees zero bounces. It filters out addresses that
  are clearly dead (bad syntax, no mail server, explicit "no such
  user" rejection) — it can't prove a mailbox will accept and someone
  will read it.

Usage:
    from email_verifier import verify_email, check_port_25
    result = verify_email("someone@example.com")
    # {"email": ..., "status": "valid"|"invalid"|"unknown", "reason": "..."}
"""

import re
import smtplib
import socket
import dns.resolver

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# A domain we control the intent for, used as the MAIL FROM during probing.
# Doesn't need to be deliverable — most servers don't verify the sender.
PROBE_FROM = "verify-probe@localhost.local"

_mx_cache = {}


def check_syntax(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def get_mx_host(domain: str, timeout: float = 5.0):
    """Return the highest-priority MX hostname for a domain, or None."""
    domain = domain.lower()
    if domain in _mx_cache:
        return _mx_cache[domain]

    host = None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        records = sorted(answers, key=lambda r: r.preference)
        if records:
            host = str(records[0].exchange).rstrip(".")
    except Exception:
        host = None

    _mx_cache[domain] = host
    return host


def check_port_25(timeout: float = 5.0) -> bool:
    """
    Quick canary: can we even open a raw connection on port 25 at all?
    Tests against Gmail's MX, which reliably accepts TCP connections.
    If this fails, your network is blocking outbound SMTP and every
    verification result below will be unreliable ("unknown").
    """
    try:
        with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=timeout):
            return True
    except Exception:
        return False


def smtp_probe(email: str, mx_host: str, timeout: float = 8.0):
    """
    Connect to the mail server and ask if it would accept mail for
    this address, without sending anything. Returns (status, reason).
    """
    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as server:
            server.ehlo_or_helo_if_needed()
            server.mail(PROBE_FROM)
            code, message = server.rcpt(email)

            if code in (250, 251):
                return "valid", "Mail server accepted the address"
            elif code in (550, 551, 553, 554):
                return "invalid", f"Mail server rejected the address (code {code})"
            else:
                return "unknown", f"Ambiguous response from mail server (code {code})"

    except smtplib.SMTPServerDisconnected:
        return "unknown", "Server disconnected during probe (common anti-spam behavior)"
    except (socket.timeout, TimeoutError):
        return "unknown", "Connection timed out (often means port 25 is blocked on this network)"
    except (ConnectionRefusedError, OSError) as e:
        return "unknown", f"Could not connect to mail server ({e})"
    except Exception as e:
        return "unknown", f"Unexpected error during probe ({e})"


def verify_email(email: str, timeout: float = 8.0):
    email = email.strip()

    if not check_syntax(email):
        return {"email": email, "status": "invalid", "reason": "Not a validly formatted email address"}

    domain = email.split("@", 1)[1]
    mx_host = get_mx_host(domain, timeout=timeout)

    if not mx_host:
        return {"email": email, "status": "invalid", "reason": "Domain has no mail server (no MX record) — this domain can't receive email"}

    status, reason = smtp_probe(email, mx_host, timeout=timeout)
    return {"email": email, "status": status, "reason": reason}


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python email_verifier.py <email>")
        sys.exit(1)

    if not check_port_25():
        print("WARNING: could not open a raw connection on port 25 — your network likely")
        print("blocks outbound SMTP. Results below may all show as 'unknown' regardless")
        print("of the real status of the address.\n")

    result = verify_email(sys.argv[1])
    print(result)
