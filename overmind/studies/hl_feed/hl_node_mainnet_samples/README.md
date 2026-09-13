# HL Non-Validating Node — Mainnet Samples (2026-06-09)

Captured from `n1` (AWS r6i.4xlarge, ap-northeast-1) within the first few
minutes of starting the Mainnet visor. General setup lives in
`../n1_provision_runbook.md`; this directory keeps the files that ship to
the node, including the Mainnet-specific `override_gossip_config.json`.

## Files in this directory

| File | Purpose |
|---|---|
| `setup_n1.sh` | OS-bootstrap script for a fresh node box |
| `visor.json` | `{"chain": "Mainnet"}` template |
| `override_gossip_config.json` | **Required for Mainnet** — Testnet auto-loads its gossip config; Mainnet does not. Snapshot of what we used. |
| `hl-visor.service`, `hl-publisher-books.service`, `hl-publisher-fills.service` | systemd unit snapshots referenced by `n1_provision_runbook.md` |
| `hl_data_retain.sh`, `hl_disk_alert.sh` | retention + alert scripts deployed to `/usr/local/bin/` on n1 |

The 200-line raw JSONL captures of `node_raw_book_diffs_streaming`,
`node_trades_streaming`, and `node_fills_streaming` that originally lived
here were used during the Mainnet-bootstrap reconnaissance to confirm the
wire format and disk burn rate; they're no longer committed. Current live
stream locations are documented in `../n1_navigation_guide.md`.

## Mainnet bootstrap delta from Testnet

1. **`override_gossip_config.json` is required** at `~/`. The node panics on
   startup without it (`error loading gossip config for non-validator:
   Missing config file`). The file we used:

   ```json
   {
     "root_node_ips": [
       {"Ip": "35.74.132.113"},
       {"Ip": "35.79.2.229"},
       {"Ip": "13.230.78.76"},
       {"Ip": "52.195.133.97"},
       {"Ip": "52.68.71.160"}
     ],
     "try_new_peers": true,
     "chain": "Mainnet",
     "reserved_peer_ips": []
   }
   ```

   The IPs are from HL's published Mainnet seed peer list (see github
   README). We picked 5 AWS-Tokyo-resident ones for low first-hop latency.
   The node will discover more peers from gossip once connected.

2. **Initial peer responses are mostly `Peer full`** — seed peers are at
   capacity; the node iterates through candidate peers received via gossip
   until one accepts.

3. **Bootstrap state height** ~1,029,570,000 (vs testnet ~585,970,000).
   Roughly 2× larger.

## Data shape

The node writes one JSON object per line under the hourly stream dirs.
Our publisher parser at `overmind/strat_main/tools/hl_node_publish.py`
works on Mainnet data directly; operate against the current `*_streaming`
paths listed in `../n1_navigation_guide.md`.

## Symbol universe (NEW vs testnet)

Many additional HIP-3 builder prefixes observed in just 200 lines of
Mainnet data:

| Prefix | Examples | Notes |
|---|---|---|
| (none) | `BTC`, `ETH`, `HYPE` | Native perps |
| `xyz:` | `xyz:SMSN`, `xyz:AMD`, `xyz:TSLA`, `xyz:SP500`, `xyz:BRENTOIL`, `xyz:XYZ100`, `xyz:DRAM`, `xyz:MRVL`, `xyz:SNDK`, `xyz:MU`, `xyz:SILVER`, `xyz:EWY` | The HIP-3 builder already in our pymultifeed whitelist |
| `cash:` | `cash:SILVER`, `cash:USA500` | NEW — cash-settled |
| `km:` | `km:MU`, `km:US500`, `km:USTECH` | NEW HIP-3 builder |
| `hyna:` | `hyna:ETH`, `hyna:FARTCOIN`, `hyna:XRP` | NEW HIP-3 builder |
| `@N` | `@1035`, `@1207`, `@1456` | Numeric IDs (seen on testnet too) |

**Implication for the node-feed publisher:** accept all syms by default
and let downstream consumers filter. The existing pymultifeed WS
configuration only whitelists `xyz:`, but the node feed will surface every
HIP-3 builder by design.

## Disk burn rate — much higher than testnet

In the first ~3 minutes after Mainnet startup, the
`node_raw_book_diffs_streaming` directory wrote **313 MB**, ≈100 MB/min ≈
**~6 GB/hour ≈ 144 GB/day**. (Testnet was ~24 GB/day.)

500 GB root disk fills in ~3.5 days at this rate. **Retention policy is
mandatory before this becomes a long-running setup.** Options:

- Rotate hourly file → gzip → drop after N hours
- Ship to S3 immediately, delete local after configurable lag
- Or just delete after the publisher has consumed (no historical retention)

The retention strategy depends on whether the **datalog machine** keeps a
canonical copy (D5) — if yes, n1 can be aggressive about deletion.
