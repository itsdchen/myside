#! /usr/bin/env python

import argparse
import asyncio
import json
import logging
import os
import requests
import smtplib
import socket
import time
from email.message import EmailMessage

"""

This currently uses a personal Gmail account to send email:
  alert.pktrade@gmail.com
Daily limit is 500 emails (rolling 24h), counting 1 per recipient, so we use a google group:
  pktrade@googlegroups.com
We have a backup email in case we hit the quota (or the account gets blocked).
TODO: Switch to Amazon SES.

"""

# Logging. This module never configures handlers itself, it just emits records.
#
# By default we log to a standalone "email_utils" logger with no handlers, so records propagate to
# the root logger. A script that configures logging (basicConfig or handlers on root) picks them up
# automatically. A script that configures nothing gets WARNING+ on stderr via Python's lastResort
# handler, and INFO is dropped -- either way stdout stays clean, which matters under cron.
#
# A script with its own named logger should nest us underneath it, e.g. in hl_margin_monitor.py:
#     email_utils.use_parent_logger(logger)
# Our records then go through "hl_margin_monitor.email_utils" and propagate into that logger's
# handlers, inheriting its level.
_LOGGER_NAME = "email_utils"
logger = logging.getLogger(_LOGGER_NAME)

def use_parent_logger(parent):
    global logger
    logger = parent.getChild(_LOGGER_NAME)
    return logger

DEFAULT_RECEIVER = "PKTrade <pktrade@googlegroups.com>"
DEFAULT_SENDER = "PKTrade Alert <alert.pktrade@gmail.com>"
LAST_EMAIL_T = {}
FALLBACK = False

# TODO:
# - Create convenience functions to wrap our emails in nice-looking
#   templates

# rate_limit blocks sending if email with same subject, receiver, and sender was just sent within
# that many minutes.
def send_mail(subject, body, receiver=DEFAULT_RECEIVER, sender=DEFAULT_SENDER, rate_limit=5,
              add_hostname=True, monospace=False):
    # Get login creds from Gmail creds file
    credsfile = "~/.creds/.Gmail.creds.json"
    with open(os.path.expanduser(credsfile)) as f:
        gmail_creds = json.load(f)
    user = gmail_creds["user"]
    password = gmail_creds["password"]
    user_bak = gmail_creds["user_backup"]
    password_bak = gmail_creds["password_backup"]

    # For multiple addresses, receiver can be comma-separated or a list
    if isinstance(receiver, list):
        receiver = ','.join(receiver)

    # Add hostname to subject so we know which machine is sending
    if add_hostname:
        subject += f" ({socket.gethostname()})"

    # Check rate limit
    key = (subject, receiver, sender)
    if rate_limit:
        if key in LAST_EMAIL_T and time.time() < LAST_EMAIL_T[key] + rate_limit * 60:
            logger.info("Email already sent in last %s min. Skipping.", rate_limit)
            return
    LAST_EMAIL_T[key] = time.time()

    # Create the email message
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    msg["Reply-To"] = DEFAULT_RECEIVER
    msg.set_content(body)
    if monospace:
        import html
        # Wrap pre in a scrollable container so wide tables horizontal-scroll
        # in clients (Gmail in particular) that would otherwise wrap them
        # despite `white-space: pre`. Drop the font down to 11px so most
        # tables fit at typical viewport widths before the scroll kicks in.
        html_body = (
            '<div style="overflow-x: auto;">'
            '<pre style="font-family: Consolas, Monaco, \'Courier New\', monospace; '
            'font-size: 11px; white-space: pre; line-height: 1.4; '
            'display: inline-block; min-width: 100%;">'
            f'{html.escape(body)}</pre></div>'
        )
        msg.add_alternative(html_body, subtype="html")

    # Function for attempting send. Returns true iff successfully sent.
    def try_send(user, password):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)
            return True
        except Exception as e:
            # Failed to send. Probably quota, maybe some other error.
            logger.warning("Failed to send email: %s", e)
            return False

    # Try to send the email. Retry with backup if failed.
    if not try_send(user, password):
        logger.warning("Could not send email as %s. Retrying as %s.", user, user_bak)
        if not try_send(user_bak, password_bak):
            global FALLBACK
            if FALLBACK: # already in fallback from failed ntfy
                logger.error("Could not send email as %s. Giving up.", user_bak)
                return
            FALLBACK = True
            logger.warning("Could not send email as %s. Redirecting to ntfy.", user_bak)
            body = "Redirected from email.\n\n" + body
            send_ntfy_alert(msg=body, title=subject, priority="urgent", to="kdb")


# Pushover alerts
PUSHOVER_USER_KEY = "uia8mkvgtu8u8f3mmab5bpwfyga7za"
# For retraded
PUSHOVER_API_TOKEN = "a8y8pepnnf5jy89hkpbhp6q3w14rbt"

def send_pushover_alert(msg: str):
    resp = requests.post("https://api.pushover.net/1/messages.json", data={
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": msg,
        "priority": 1,        # high priority (repeats until acknowledged)
        "retry": 60,          # retry every 60s
        "expire": 3600        # keep retrying up to 1 hour
    })
    if resp.status_code != 200:
        logger.error("Pushover error: %s", resp.text)

NTFY_PRIORITIES = ["min", "low", "default", "high", "urgent"]
NTFY_TOPICS = {"dchen": "pktrade-alerts-dchen",
               "kdb": "pktrade-alerts-kdb",
               "all": "pktrade-alerts-1792"}
NTFY_SERVER = "https://ntfy.l1group.xyz" # Self-hosted, unlimited.
NTFY_SERVER_BAK = "https://ntfy.sh" # ntfy's public server. Limited to 250/day. Pay for more.
LAST_NTFY_T = {}

# Setup Instructions (for Android):
# 1. Install ntfy app on phone.
# 2. In app: subscribe to topics above, as appropriate, on both servers.
# 3. In app (optional): turn on "Keep alerting for highest priority", customize loudness and
#    vibration per priority level.
# 4. In phone: allow ntfy to bypass all battery optimizations and DND
def send_ntfy_alert(msg, title=None, priority=None, to="all", rate_limit=1):
    topic = NTFY_TOPICS[to]
    headers = {}
    if title:
        headers["Title"] = title.encode("utf-8")
    if priority:
        if priority in NTFY_PRIORITIES:
            headers["Priority"] = priority
        else:
            logger.warning("Invalid ntfy priority (%s). Using default.", priority)

    # Check rate limit based on priority, to, and either title (if any) or msg
    key = (title, priority, to) if title else (msg, priority, to)
    if rate_limit:
        if key in LAST_NTFY_T and time.time() < LAST_NTFY_T[key] + rate_limit * 60:
            logger.info("Ntfy already sent in last %s min. Skipping.", rate_limit)
            return
    LAST_NTFY_T[key] = time.time()

    # Function for attempting send. Returns true iff successfully sent.
    def try_send(server):
        try:
            url = f"{server}/{topic}"
            r = requests.post(url, data=msg.encode("utf-8"), headers=headers, timeout=3)
            if r.status_code != 200:
                raise Exception(f"{r.status_code} {r.text}")
            return True
        except Exception as e:
            # Failed to send. Could be server, quota, or other issues.
            logger.warning("Failed to send ntfy via %s: %s.", server, e)
            return False

    # Try to send the alert. Retry with backup if failed.
    if not try_send(NTFY_SERVER):
        logger.warning("Could not send ntfy via %s. Retrying with %s.",
                       NTFY_SERVER, NTFY_SERVER_BAK)
        if not try_send(NTFY_SERVER_BAK):
            global FALLBACK
            if FALLBACK: # already in fallback from failed emails
                logger.error("Could not send ntfy via %s. Giving up.", NTFY_SERVER_BAK)
                return
            FALLBACK = True
            logger.warning("Could not send ntfy via %s. Redirecting to email.", NTFY_SERVER_BAK)
            msg = "Redirected from ntfy.\n\n" + msg
            send_mail(subject=title, body=msg, receiver="dbkhanh@gmail.com")

def send_alerts(subject, body, rate_limit=None):
    """
    Send email and ntfy alerts in a separate fire-and-forget thread without blocking.
    If called from a non-async program (with no event loop running), just send blocking version.
    If rate_limit given, use that for both mail and ntfy, otherwise leave defaults.
    """
    def _send():
        logger.info("Sending email and ntfy alerts!")
        try:
            if rate_limit is None:
                send_mail(subject=subject, body=body)
            else:
                send_mail(subject=subject, body=body, rate_limit=rate_limit)
        except Exception as e:
            logger.error("Email got error: %s", e)
        try:
            if rate_limit is None:
                send_ntfy_alert(msg=subject)
            else:
                send_ntfy_alert(msg=subject, rate_limit=rate_limit)
        except Exception as e:
            logger.error("Ntfy got error: %s", e)
    try:
        asyncio.get_running_loop() # just check if there's a running loop
        asyncio.create_task(asyncio.to_thread(_send))
    except RuntimeError: # no loop, probably called from non-async program
        _send()
    except Exception as e:
        logger.error("Email/ntfy got error: %s", e)

def main():
    """Test sending email or push notification."""
    # Run standalone, so configure root and show our INFO lines (on stderr, per basicConfig).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    parser = argparse.ArgumentParser()

    # email args
    parser.add_argument("--email", nargs='?', const=DEFAULT_RECEIVER, default=None)
    parser.add_argument("--subject")
    parser.add_argument("--body")

    # ntfy args
    parser.add_argument("--ntfy", nargs='?', const="all", default=None)
    parser.add_argument("--msg")
    parser.add_argument("--title")
    parser.add_argument("--priority", default="default", choices=NTFY_PRIORITIES)

    args = parser.parse_args()

    if args.email:
        if not args.subject or not args.body:
            print("Email requires subject and body.")
            return
        send_mail(subject=args.subject, body=args.body, receiver=args.email)
    if args.ntfy:
        if not args.msg:
            print("Ntfy requires msg.")
            return
        send_ntfy_alert(msg=args.msg, title=args.title, priority=args.priority, to=args.ntfy)

if __name__ == "__main__":
    main()
