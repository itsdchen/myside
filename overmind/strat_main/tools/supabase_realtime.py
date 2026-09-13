#! /usr/bin/env python

import argparse
import asyncio
import json
import os
import sys
import time

from tenacity import retry, wait_fixed, retry_if_exception_type

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils
from util import sbcreds

# Reuse the canonical um_id -> name mapping so alerts are human-readable.
from usermsg import UM_ID_MAP
UM_NAME_BY_ID = {v: k for k, v in UM_ID_MAP.items()}

# Force a reconnect if we haven't received any usermsg in this long. We rely on the
# hourly no-op usermsg to prove the connection is still delivering; if even that stops
# arriving, the subscription has silently gone dead (e.g. Supabase weekend maintenance)
# and we tear down and reconnect.
WATCHDOG_SECS = 61 * 60
# How often the watchdog checks.
WATCHDOG_CHECK_SECS = 30

class ForceRestart(Exception):
    """Raised to trigger the @retry reconnect (e.g. the watchdog timing out)."""

def should_alert(user, um_name, strat_id_regex, sym):
    """Manual filters if desired. Returns dict: {"email": True/False, "ntfy": True/False/prio}
       where prio is a ntfy priority string ("min", "low", "default", "high", "urgent").
       True defers to alert_cfg, and False/prio overrides it."""
    email_alert = True
    ntfy_alert = True
    # Suppress the hourly no-op UM.
    if strat_id_regex == "noexist":
        email_alert = False
        ntfy_alert = False
    return {"email": email_alert, "ntfy": ntfy_alert}

# alert_targets for operational (reconnect/watchdog/error) alerts: always use both
# configured channels, at the configured priority.
ALL_TARGETS = {"email": True, "ntfy": True}

def notify(alert_cfg, alert_targets, subject, body):
    """Print, and send email/ntfy alerts via the configured channels, gated by
    alert_targets (a should_alert()-style dict {"email": bool, "ntfy": bool|priority}).
    A truthy value defers to alert_cfg's channel; an ntfy priority string overrides the
    configured priority. Pass ALL_TARGETS for operational alerts."""
    print(f"{subject}\n{body}")
    if not alert_cfg:
        return
    if alert_targets["email"] and alert_cfg["email"] is not None:
        try:
            email_utils.send_mail(subject=subject, body=body, receiver=alert_cfg["email"],
                                  rate_limit=alert_cfg["rate_limit"], add_hostname=False)
        except Exception as e:
            print(f"  email error: {e}")
    if alert_targets["ntfy"] and alert_cfg["ntfy"] is not None:
        try:
            priority = alert_cfg["priority"]
            if alert_targets["ntfy"] in email_utils.NTFY_PRIORITIES:
                priority = alert_targets["ntfy"]
            email_utils.send_ntfy_alert(msg=body, title=subject, priority=priority,
                                        to=alert_cfg["ntfy"], rate_limit=alert_cfg["rate_limit"])
        except Exception as e:
            print(f"  ntfy error: {e}")

def force_restart(alert_cfg, reason):
    """Alert on the reconnect (via the usermsg alert channels), then raise ForceRestart
    to trigger listen_for_usermsgs's @retry."""
    notify(alert_cfg, ALL_TARGETS, "USERMSG LISTENER reconnecting",
           f"Reconnecting: {reason}\n")
    raise ForceRestart(reason)

# Supabase Setup for Usermsgs
# Create 2 tables called "mainnet_usermsgs_1" and "testnet_usermsgs_1" with the following settings:
#   - Enable Row Level Security (RLS) --> this is on by default
#   - Enable Realtime --> this is off by default
# Give it the following columns:
#   - id             : int8      : Primary, Is Identity
#   - created_at     : timestamp : default now()
#   - user           : text      :
#   - strat_id_regex : text      :
#   - sym            : text      : default ""
#   - um_id          : int4      :
#   - args           : json      : IsNullable
# For each table, create 2 RLS policies:
#   1. create policy "allow public read-only"
#      on "public"."<prefix>_usermsgs_1"
#      as PERMISSIVE
#      for SELECT
#      to public
#      using (
#        true
#      );
#   2. create policy "allow auth all"
#      on "public"."<prefix>_usermsgs_1"
#      as PERMISSIVE
#      for ALL
#      to authenticated
#      using (
#        true
#      );

def handle_usermsg(payload, alert_cfg=None, state=None):
    # Any received message (including the hourly no-op) proves the connection is still
    # delivering, so reset the watchdog before anything else.
    if state is not None:
        state["last_msg_t"] = time.monotonic()
    print("New message received!")
    # print(f"Payload: {payload}")
    um = payload["data"]["record"]
    user           = um["user"]
    strat_id_regex = um["strat_id_regex"]
    sym            = um["sym"]
    um_id          = um["um_id"]
    args           = um["args"]
    um_name        = UM_NAME_BY_ID.get(um_id, str(um_id))
    print(f"Usermsg received: user:{user}, strat_id_regex:{strat_id_regex}, " +
          f"sym:{sym}, um:{um_name}({um_id}), args:{args}")

    if not alert_cfg:
        return
    alert_targets = should_alert(user, um_name, strat_id_regex, sym)
    if not alert_targets["email"] and not alert_targets["ntfy"]:
        print("  -> filtered out, no alert")
        return

    subject = f"UM {um_name} from {user}"
    body = (f"user: {user}\n"
            f"strat_id_regex: {strat_id_regex}\n"
            f"sym: {sym}\n"
            f"um: {um_name} ({um_id})\n"
            f"args: {json.dumps(args)}\n")
    notify(alert_cfg, alert_targets, subject, body)

# Retry (a fresh client from scratch) only on ForceRestart, i.e. a watchdog timeout.
# Unexpected exceptions (including a subscribe timeout) are not retried: they fall through
# to print + alert + exit, as before.
@retry(retry=retry_if_exception_type(ForceRestart))
async def listen_for_usermsgs(testnet, credsfile, alert_cfg=None):
    # Create async Supabase client (must be awaited)
    creds = sbcreds.SupabaseCredentials(testnet, credsfile)
    table_name = creds.get_table_name("usermsg")
    client = await creds.get_client()

    print(f"Connecting to Supabase realtime...")
    print(f"Listening for INSERT events on table: {table_name}")
    print("Press Ctrl+C to stop")
    print("-" * 50)

    # Shared with handle_usermsg: last time we received any usermsg, for the watchdog.
    # Fresh each (re)connect, so a slow first message after a reconnect doesn't re-trip it.
    state = {"last_msg_t": time.monotonic()}
    try:
        # Create a channel (arbitrary name)
        channel = client.channel("usermsgs")

        # Set up the postgres changes listener for INSERT events
        channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table=table_name,
            callback=lambda payload: handle_usermsg(payload, alert_cfg, state)
        )

        # Subscribe to the channel (must be awaited). A timeout here propagates to the
        # outer handler below, which alerts and exits.
        await asyncio.wait_for(channel.subscribe(), timeout=2.0)
        print("Successfully subscribed to realtime changes!")

        # Watchdog loop (replaces the plain keep-the-script-running sleep loop). If we go
        # WATCHDOG_SECS without any usermsg -- the hourly no-op should arrive well within
        # that -- assume the subscription silently died and force a reconnect.
        while True:
            await asyncio.sleep(WATCHDOG_CHECK_SECS)
            silent_for = time.monotonic() - state["last_msg_t"]
            if silent_for >= WATCHDOG_SECS:
                force_restart(alert_cfg, f"no usermsg received for {silent_for / 60:.0f}min")

    except asyncio.CancelledError:
        print("Interrupted")
    except ForceRestart:
        raise  # already alerted by force_restart(); trigger the @retry
    except Exception as e:
        print(f"Error: {e}")
        notify(alert_cfg, ALL_TARGETS, "USERMSG LISTENER died",
               f"Listener exiting on unexpected error: {e!r}\n")
    finally:
        print("Cleaning up...")
        try:
            await channel.unsubscribe()
            print("Unsubscribed from channel")
            await client.realtime.close()
            print("Closed websocket")
        except Exception as e:
            print(f"Error unsubscribing or closing socket: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="Subscribe to usermsgs using Supabase realtime.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--testnet", action="store_true",
        help="Use testnet tables instead of mainnet"
    )
    parser.add_argument(
        "--email", nargs="?", const=email_utils.DEFAULT_RECEIVER, default=None,
        help="Send email alerts (optional receiver override)"
    )
    parser.add_argument(
        "--ntfy", nargs="?", const="all", default=None,
        choices=list(email_utils.NTFY_TOPICS.keys()),
        help="Send ntfy alerts to this topic"
    )
    parser.add_argument(
        "--priority", default="default", choices=email_utils.NTFY_PRIORITIES,
        help="ntfy priority"
    )
    parser.add_argument(
        "--rate-limit", type=int, default=0,
        help="Suppress repeats of the same alert within this many minutes (0 = off)"
    )
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    # Only alert if a channel was requested; otherwise just print (original behavior).
    alert_cfg = None
    if args.email is not None or args.ntfy is not None:
        alert_cfg = {"email": args.email, "ntfy": args.ntfy,
                     "priority": args.priority, "rate_limit": args.rate_limit}

    asyncio.run(listen_for_usermsgs(args.testnet, args.sb_creds, alert_cfg))

if __name__ == "__main__":
    main()
