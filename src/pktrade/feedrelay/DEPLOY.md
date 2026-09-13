# pkrelay + RelayDBCME deployment

This directory ships a CME market data relay (`pkrelay`, runs in us-east-2)
that forwards databento MBP-1 quotes as UDP datagrams to pkmultifeed
consumers (runs in Tokyo). Goal: avoid the ~10s burst stalls seen on the
direct Tokyo↔databento TCP connection by running the fragile short-path
TCP leg close to databento and shipping over AWS backbone as UDP.

Architecture:

    [CME] → [databento CME gateway]
               ↓ TCP (~15ms RTT, clean)
          [pkrelay on us-east-2]
               ↓ UDP × N destinations (AWS backbone, ~150ms)
          [pkmultifeed on Tokyo-1] + [Tokyo-2] + [Tokyo-3]
                                      ↓
               existing direct-TCP path also still connected;
               cmeFusionState CAS gate dedupes events so each
               CME event is published once to downstream.

## AWS infra (one-time)

### 0. Background if you haven't done VPC work before

Every EC2 instance lives inside a **VPC**, an isolated private network.
Each AWS region auto-creates a "default VPC" so you've never had to
think about it. For this deployment there's one thing you do have to
think about: the Tokyo consumers and the us-east-2 relay need to reach
each other by **private IP** over AWS backbone (VPC peering), and
**peering requires non-overlapping CIDR ranges between the two VPCs**.

AWS's default VPCs in every region all use `172.31.0.0/16`, so if you
just use the defaults on both ends, peering will fail with a CIDR
overlap error. The fix: create a dedicated non-default VPC in us-east-2
with its own non-overlapping range. Your Tokyo consumers can stay
in whatever VPC they're already in (almost certainly Tokyo's default
`172.31.0.0/16`).

### 1. Create the us-east-2 VPC

1. AWS console → switch region to **Ohio (us-east-2)**.
2. VPC console → "Create VPC" → "VPC and more" template.
3. Name: `pkrelay-vpc`. IPv4 CIDR: `10.100.0.0/16` (any non-overlapping
   range works; this one avoids Tokyo's default).
4. Availability Zones: 1. Public subnets: 1. Private subnets: 0.
5. NAT gateways: None. VPC endpoints: None. Leave other defaults.
6. Create. Takes ~30 seconds.

### 2. Launch the relay host in that VPC

`c6g.medium` (ARM, single-digit CPU load, ~$17/mo) in `pkrelay-vpc`'s
public subnet. Assign a public IPv4 so you can SSH in. Note the
instance's private IP — it'll be in `10.100.x.x` and is the source
address that Tokyo will see packets from.

### 3. Find the Tokyo VPC CIDR

Tokyo region → VPC console → Your VPCs → look at the default VPC's
"IPv4 CIDR" column. Probably `172.31.0.0/16`. You'll plug this into
us-east-2's route table; us-east-2's CIDR (`10.100.0.0/16`) will
likewise go into Tokyo's.

### 4. VPC peering Tokyo ↔ us-east-2

- us-east-2 VPC console → Peering Connections → Create.
  - Requester VPC: `pkrelay-vpc`.
  - Accepter: "Another region", select Tokyo, enter Tokyo VPC ID.
  - Create.
- Switch to Tokyo region → Peering Connections → find the pending
  request → Actions → Accept.
- Now add route-table entries:
  - us-east-2 `pkrelay-vpc`'s main route table: add
    `destination = <Tokyo CIDR>, target = <peering connection>`.
  - Tokyo default VPC's main route table: add
    `destination = 10.100.0.0/16, target = <peering connection>`.

### 5. Security groups

- Each Tokyo consumer's SG: allow inbound UDP on the chosen port
  (4242 below) from `10.100.0.0/16` (the us-east-2 VPC CIDR). Not
  from the us-east-2 SG ID — inter-region peering can't reference SG
  IDs across regions.
- us-east-2 relay host SG: allow outbound UDP (usually open by
  default) and outbound TCP/443 to databento. Inbound from your
  office IP for SSH as usual.

### 6. Sanity check

From the us-east-2 box:

    ping -c 3 <tokyo-private-ip>
    traceroute <tokyo-private-ip>

Expect ~150 ms RTT and a traceroute showing only AWS-internal hops
(no public-internet networks). If ping times out, work through: is
the peering connection "Active" (not "Pending Acceptance")? Are
both route tables updated? Do the SGs allow the traffic?

You don't have to do all six steps before the next sections, but
packets won't actually flow until peering is up and the route
tables / SGs are in place.

## Relay host setup (us-east-2)

1. **Copy binary and config.**

        scp bin/pkrelay us-east:/home/ubuntu/bin/
        scp src/pktrade/feedrelay/pkrelay_config.example.json \
            us-east:/home/ubuntu/etc/pkrelay_config.json
        scp ~/.creds/.DataBento.creds.json us-east:/home/ubuntu/.creds/

2. **Edit `/home/ubuntu/etc/pkrelay_config.json`** with the Tokyo
   consumer destinations. Symbol list should be the UNION of what any
   Tokyo consumer needs — each consumer's `RelayDBCME.symbols` then
   picks its subset.

   **Use the Tokyo consumers' PRIVATE IPs, not their public IPs.**
   VPC peering only routes traffic between private addresses on both
   sides; that's the whole mechanism keeping the traffic on AWS
   backbone. Public IPs would send packets out to the public internet
   and defeat the point of this architecture — and security groups
   keyed on the us-east-2 VPC CIDR won't match public-IP traffic.

   To find each Tokyo consumer's private IP:
   - EC2 console → the instance → Networking tab → "Private IPv4
     addresses", or
   - `ip -4 addr show` on the instance itself (typically `172.31.x.x`
     in a default VPC, `10.x.x.x` in a custom one).

   After writing the config, sanity-check connectivity from us-east-2:

        ssh us-east 'ping -c 3 <tokyo-private-ip>'
        ssh us-east 'traceroute <tokyo-private-ip>'

   Expect ~150 ms RTT and a traceroute that shows only AWS-internal
   hops (no public-internet hostnames). If ping fails or traceroute
   leaves AWS, the VPC peering / route tables are wrong — fix that
   before starting pkrelay.

3. **Install systemd unit.**

        scp src/pktrade/feedrelay/pkrelay.service us-east:/tmp/
        ssh us-east 'sudo mv /tmp/pkrelay.service /etc/systemd/system/ \
                     && sudo systemctl daemon-reload \
                     && sudo systemctl enable --now pkrelay'

4. **Watch startup logs.**

        ssh us-east 'journalctl -u pkrelay -f'
        # or tail the glog files under /home/ubuntu/logs/pkrelay/

   Expected within seconds:

        pkrelay: trading date YYYYMMDD
        pkrelay: subscribing CLN6 (base CL, weight 1)
        pkrelay: forwarding to 3 destination(s)
        pkrelay: databento client started
        pkrelay SymbolMapping: CLN6 -> <iid>
        pkrelay CLN6 msgtime ... delay Xms seq N    (every 1000 msgs)

5. **Schedule a daily restart.** `pkrelay --date TOMORROW` resolves
   once at startup, so the dated contract set is frozen for one
   trading day. You must restart the unit around CME session roll
   (18:00 ET / 23:00 UTC) so the next day's contracts pick up.

   Either a systemd timer, or a cron entry matching however
   pkmultifeed itself is cycled on your infra. Example timer:

        # /etc/systemd/system/pkrelay-daily.timer
        [Unit]
        Description=Restart pkrelay at CME session roll

        [Timer]
        OnCalendar=*-*-* 23:00:00 UTC
        Persistent=true
        Unit=pkrelay-restart.service

        [Install]
        WantedBy=timers.target

        # /etc/systemd/system/pkrelay-restart.service
        [Unit]
        Description=Restart pkrelay
        [Service]
        Type=oneshot
        ExecStart=/bin/systemctl restart pkrelay

## Tokyo consumers

On each of the three pkmultifeed hosts, add a top-level block to the
feed config alongside `DataBentoCME`:

    "RelayDBCME": {
        "symbols": ["CL", "HG", "NG"],
        "listen_port": 4242
    }

Then restart pkmultifeed. Expected log lines:

    RelayDBCME: listening on UDP :4242
    RelayDBCME: registered CLN6 (base CL, weight 1)
    RelayDBCME CLN6 msgtime ... delay Xms relay_transit Yms seq N drops 0

- `delay` is wallclock minus CME event time (same shape as the
  existing TCP path's `tot_delay`).
- `relay_transit` is (receive wallclock) − (relay's send wallclock),
  i.e. the AWS-backbone transit time for the datagram. Expect ~150ms
  steady, close to your Tokyo↔us-east-2 RTT.
- `drops` is a best-effort sequence-gap heuristic, not a perfect loss
  counter. The relay emits both quotes and heartbeats from one shared
  `seq` space, and UDP can reorder packets, so a non-zero value means
  "we observed a gap in arrival order" rather than "network definitely
  lost N packets". It is still useful as a rough health signal: if it
  trends up hard during bursts, suspect kernel recv-buffer overflow or
  path loss. But don't read small non-zero values as forensic proof of
  packet loss.

## Fused-feed semantics

The Tokyo consumer keeps both CME paths live at once:

- direct databento TCP (`DataBentoCME`)
- relayed UDP (`RelayDBCME`)

Both paths feed the same `processCmeQuote()` logic, and a CAS gate
dedupes candidate publishes by `(our_sym, ts_event_ms)`. Operationally:

- Newer event time wins.
- Equal event time is treated as a duplicate.
- We optimize for freshness, not reliable delivery or full packet
  reconstruction.

That means if one path drops a packet but a newer intact packet arrives
on either path, the newer one is what gets published. This is
intentional: the fused feed is a best-effort "latest good quote" stream.

For roll blends there is one extra subtlety: dedupe keys on the merged
event timestamp only, not a full diff of the blended quote payload. So
if the blend payload changes while the merged timestamp stays equal, the
later candidate publish is considered a duplicate and dropped. That
ambiguity is accepted in the current design.

## Optional fusion stats

`pkmultifeed` also has an optional debug flag:

    --cme-fusion-stats

When enabled, it records per-(symbol, source) counters for:

- events seen at the fusion gate
- CAS wins
- average publish delay on wins
- max publish delay on wins

and emits a periodic `LOG(INFO)` dump every 5 minutes plus one final
dump on shutdown.

This is off by default. In normal production mode the hot path skips the
detailed accounting and you still have the per-message delay logs from
both the direct TCP and relay UDP paths.

## Canary and rollout

1. Enable `RelayDBCME` on ONE Tokyo box first. Leave the other two on
   TCP-only.
2. Run for a day. Check:
   - `drops` stays near zero.
   - During CME bursts, the RelayDBCME `delay` is much lower than the
     TCP `tot_delay` (the whole point — the relay sidesteps the
     long-fat-network stall).
   - The canary box's published BBO stream is consistent with the
     TCP-only boxes (no skew, no duplicates — CAS gate is working).
3. Enable on the remaining two boxes.
4. Watch pager patterns. You should see fewer `CME FEED STALE` emails
   (UDP path beats TCP during bursts, keeps the fused feed fresh) and
   zero `RELAY CME SILENT` emails (relay is healthy). If you start
   seeing `RELAY CME SILENT`, check the relay host / VPC peering, not
   the Tokyo box.

## Operational notes

- **Paging is per-path by design.** TCP path still alerts on its own
  `CME FEED STALE` when stuck; UDP path alerts on `RELAY CME SILENT`
  when no packets arrive. Each is independent — if one path is dead
  and the other covers, you still want to know the dead one is dead
  (reduced redundancy).
- **Stopping pkrelay on us-east-2 is safe in production.** Tokyo
  consumers fall back to their existing TCP path. You will start
  seeing `CME FEED STALE` emails again during bursts, but nothing
  breaks.
- **Duplicates should never reach downstream.** The `cmeFusionState`
  CAS gate inside `processCmeQuote` runs in both the TCP callback and
  the UDP receiver; whichever path wins the CAS for a given (our_sym,
  ts_event_ms) is the one that publishes, the other drops.
- **Sequence numbering is per-relay-process.** A `pkrelay` restart
  resets `seq`; consumers detect the backwards jump and resync
  without inflating the drop counter.

## Files in this directory

- `pkrelay_packet.h` — wire format. Bump `kPkRelayVersion` on any
  layout change; consumers reject non-matching versions.
- `pkrelay.cc` — relay binary. Dumb by design: no blend-leg merge,
  no session filter, no symbol mapping, no protobuf. All downstream
  logic lives in pkmultifeed.
- `pkrelay.service` — sample systemd unit for the us-east-2 host.
- `pkrelay_config.example.json` — sample relay config.
- `DEPLOY.md` — this file.
