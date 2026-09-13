# AWS Setup — Tokyo Trading Fleet (2026-06-10)

Snapshot of our AWS footprint for a cost-reduction review. Account `l1`
(228948462720), region `ap-northeast-1` (Tokyo). Single VPC
`vpc-05ff5da2c2153c852` (CIDR `172.31.0.0/16`, default VPC).

## Instance inventory

| Name | InstanceId | Type | State | Purpose | Launch |
|---|---|---|---|---|---|
| `gf0` | i-028d910730ae3275b | c7i.xlarge | running | trading box (gf strat family) | 2026-01-28 |
| `gf1` | i-07dced6a173b4e4a3 | c7i.xlarge | running | trading box | 2026-02-07 |
| `gf2` | i-090913f59e38a378b | c7i.xlarge | running | trading box | 2026-02-07 |
| `gf3` | i-019f098138ea28b53 | c7i.xlarge | running | trading box | 2026-02-19 |
| `gv0` | i-0ea871e84bd455fcb | c7i.xlarge | running | trading box (gv variant) | 2026-04-20 |
| `qf1` | i-0130cd80418149d73 | c7i.large  | running | trading box (smaller) | 2026-05-18 |
| `split-box` | i-0cfba86821edf2d12 | t2.large | running | data/splitter utility | 2026-03-12 |
| `n1` | i-0fade2737acb92dd2 | r6i.4xlarge | running | HL non-validating node (new, 2026-06-09) | 2026-06-09 |
| (no name) | i-002c1352d26835817 | c5.xlarge | **stopped** | unclear, has EIP `MYIP1` attached | 2026-02-11 |
| `test-multifeed-receive` | i-03e1089c5651cdd2a | t3.micro | **stopped** | one-off test box | 2026-04-20 |
| `relayrec` | i-0e6b08f2033197395 | c6i.large | **stopped** | feed relayer (likely retired) | 2026-04-20 |

7 running, 3 stopped, 1 newly added.

## EBS volumes

| VolumeId | Type | Size | Attached to |
|---|---|---|---|
| vol-0e41a927584c1db6f | gp2 | 50 GB | gf0 |
| vol-0b3bbf110c2cd1a36 | gp2 | 50 GB | gf1 |
| vol-03e6a2d288e25c961 | gp2 | 50 GB | gf2 |
| vol-072583e7ac495f428 | gp2 | 50 GB | gf3 |
| vol-0d081b189def4775d | gp2 | 50 GB | gv0 |
| vol-0f8cb832b4a61b261 | gp3 | 40 GB | qf1 |
| vol-05b2662a50f9e22f4 | gp2 | 500 GB | split-box |
| vol-0c44ae76668ec50f5 | gp3 | 500 GB | n1 |
| vol-07351e028adb4973b | gp2 | 50 GB | stopped c5.xlarge |
| vol-0df618101e72d101f | gp2 | 16 GB | stopped relayrec |
| vol-051b9be322ee1f9af | gp3 | 12 GB | stopped test-multifeed-receive |

Observation: **most trading-box root volumes are still `gp2`**; only the
newer ones (qf1, n1) use `gp3`. EBS volumes attached to stopped instances
still bill for the full provisioned size.

## Elastic IPs

| Allocation | IP | Attached to | Notes |
|---|---|---|---|
| eipalloc-0dad09684890f4322 | 13.115.191.76 | stopped c5.xlarge | tagged `MYIP1` — EIP attached to a stopped instance is **billed** |
| eipalloc-0080b8803dabaa038 | 13.159.159.156 | gf0 (running) | free (attached + running) |
| eipalloc-0dd58d342491dcf7b | 18.177.53.201 | gf1 (running) | free |
| eipalloc-0d5ee31b2b6a73d1c | 35.74.180.231 | gf2 (running) | free |
| eipalloc-0319a19b1cd1321fa | 54.95.168.171 | gf3 (running) | free |

EIP limit is 5 (default) — already at cap. Also note `gv0`, `qf1`, and
`n1` run without EIPs (use auto-assigned public IPs).

## IAM / access

- IAM user `dchen` (used for CLI access). Currently has
  `AmazonEC2FullAccess` attached. No `iam:*` or `servicequotas:*` —
  service-quota requests go through console-as-root.
- Console login uses the root account (account email holder).
- SSH key pair: `l1_dchen` (~/aws_pairs/l1_dchen.pem on the dev box, used
  for almost every box).
- AWS profiles on the dev box: `default` (now points at l1) and `old` (an
  unrelated previous-org account, renamed away).

## Networking

- Single VPC (`vpc-05ff5da2c2153c852`, CIDR `172.31.0.0/16`, default).
- Most boxes in subnet `subnet-0b8aecc4285e4208e`, AZ `ap-northeast-1a`.
- Security groups: per-instance setup. Trading boxes share
  `sg-016044649e311374a` (the historical group). The new HL node has its
  own SG `sg-071eccd3018164ebe` because it needs public 4001-4002 ingress
  that doesn't belong on trading boxes.

## Quotas

- vCPU limit (Standard family A/C/D/H/I/M/R/T/Z) raised from 36 → **80**
  on 2026-06-09 to accommodate the new r6i.4xlarge node (16 vCPU) on top
  of the trading fleet (~24 vCPU). Current utilization ≈40/80.
- EIP limit: still default **5/5** — fully consumed.

## Cost optimization candidates

### Quick wins (no architecture change)

1. **No Savings Plan in place.** The trading fleet has been running 24/7
   for months. An EC2 Instance Savings Plan scoped to
   `c7i / ap-northeast-1 / Linux / shared` at the right commit level
   would discount ~40-50%. Rough math for the 5×c7i.xlarge + 1×c7i.large
   trading fleet:
   - On-demand: ~$0.21/hr × 5 + ~$0.106/hr ≈ $1.16/hr → ~$840/mo
   - 1-yr No-Upfront Instance SP (~40% off): ~$0.70/hr → **~$510/mo,
     save ~$330/mo**
   - 1-yr All-Upfront slightly better.
   - Similarly for the n1 r6i.4xlarge (~$780/mo on-demand → ~$465/mo
     under r6i SP, save ~$315/mo).
   - **Combined potential savings: ~$640/mo (~$7.7k/yr)** before stopped
     boxes or storage tweaks.
   - Plan to commit AFTER ~2 weeks of validating n1 stays as r6i.4xlarge
     (don't lock the family if we'll resize).

2. **Stopped instances still billing for EBS + EIP.**
   - `MYIP1` EIP on the stopped c5.xlarge: ~$3.60/mo wasted. Either start
     the instance or release the EIP. The `MYIP1` name implies someone
     wanted a specific IP — verify before releasing.
   - 50 GB gp2 on stopped c5: ~$5/mo. If the box is truly retired,
     terminate it (snapshot first if there's anything worth keeping).
   - 12 GB gp3 on stopped test-multifeed-receive: ~$1/mo. Cheap but
     pointless if the test is done.
   - 16 GB gp2 on stopped relayrec: ~$1.60/mo. Probably retired.
   - Total: ~$11/mo. Low absolute dollars but tidies the account.

3. **gp2 → gp3 migration on running boxes.** Trading boxes are 50 GB gp2
   each. gp3 is **cheaper** ($0.096 vs $0.10/GB-mo) AND has 3000 IOPS
   baseline (vs gp2's 150 IOPS at 50 GB). Live-migration is non-
   disruptive (`aws ec2 modify-volume`). Per-volume savings tiny, but
   it's strictly better for both cost AND performance. ~$0.40/mo/volume.

4. **t2.large `split-box` with a 500 GB gp2 root**. t2 is the oldest
   burstable family; t3.large is **cheaper** ($0.062 vs $0.067/hr in
   Tokyo) AND faster (Nitro-based). Same vCPU/RAM. If `split-box` isn't
   I/O-heavy on 500 GB the volume could shrink too (or move to gp3 for
   $20/mo savings on the volume alone). Quick t2→t3 migration via stop +
   change-instance-type + start, ~5 min.

### Worth investigating

5. **Is `qf1` (c7i.large) sized right?** It's the only "large" trading
   box (others are xlarge). If it's the same workload as gf*, either it's
   under-resourced (bad) or the others are over-resourced (saving
   opportunity). Worth comparing CPU/memory utilization.

6. **EIP usage pattern.** The 4 trading boxes that have EIPs are
   gf0-gf3. gv0 and qf1 run without. If EIPs are only there for stable
   SSH/exchange-whitelist reasons, are they still needed for all 4?
   Releasing 1-2 EIPs frees quota for new boxes and saves a couple
   dollars on the `MYIP1`-style stopped scenario.

7. **Are the 3 stopped instances retired?** Talk to David before
   terminating — but `relayrec` and `test-multifeed-receive` look like
   bygone experiments. Termination clears the EBS too.

8. **`split-box`'s 500 GB volume.** That's a big chunk of EBS. Is the
   data still needed? Could it move to S3 with lifecycle rules instead
   of being kept on EBS?

### Not recommended right now

- **Spot instances.** Trading boxes need predictable uptime; Spot
  termination is a no-go for live trading.
- **Reserved Instances** (old-style). Savings Plans are strictly more
  flexible for the same discount, just use those.
- **Cross-region moves.** Tokyo location is load-bearing for HL latency.

## Rough monthly bill estimate (very approximate)

| Component | Cost |
|---|---|
| 5× c7i.xlarge running 24/7 | ~$765 |
| 1× c7i.large running 24/7 | ~$76 |
| 1× r6i.4xlarge (n1) running 24/7 | ~$780 |
| 1× t2.large (split-box) running 24/7 | ~$48 |
| EBS gp2 volumes (~950 GB total) | ~$95 |
| EBS gp3 volumes (~552 GB total) | ~$53 |
| 1× EIP on stopped instance | ~$4 |
| **Total** | **~$1820/mo** |

(Numbers are public on-demand pricing for ap-northeast-1, before any
data transfer / snapshots / other line items. Real bill is the source of
truth — pull `aws ce get-cost-and-usage` for actuals.)

## Suggested order of operations

1. Verify the 3 stopped instances are truly retired; terminate them and
   release `MYIP1` if it isn't needed. Frees quota + saves ~$11/mo
   (and an EIP slot).
2. Migrate trading-box root volumes gp2 → gp3 in place (no downtime).
   Tiny savings but strictly better performance.
3. After ~2 weeks of n1 running stably as r6i.4xlarge: buy two
   1-year No-Upfront EC2 Instance Savings Plans:
   - `c7i / ap-northeast-1 / Linux / shared` sized to cover current
     trading fleet usage (~$1/hr commit covers all c7is).
   - `r6i / ap-northeast-1 / Linux / shared` at ~$0.68/hr for n1.
   - Combined savings ~$640/mo.
4. Investigate split-box sizing + storage; consider t2→t3 swap.
5. Quarterly review whether `qf1` should be xlarge or others should be
   large.

## Actions taken — 2026-06-10

### Purchased: c7i EC2 Instance Savings Plan

- **Type**: EC2 Instance SP (not Compute SP — c7i family is stable in
  Tokyo, max discount preferred)
- **Family / Region**: `c7i / ap-northeast-1`
- **Term / Payment**: 1-year / No upfront
- **Hourly commitment**: $1.07/hr
- **Cost**: $781.10/mo, $9,373.20/yr
- **Estimated savings**: $276/mo (~$3.3k/yr)
- **Actual discount**: **23%**, not the ~40% estimated earlier in this
  doc. The earlier estimate was too optimistic; revise future
  projections accordingly.

Purchase Analyzer at $1.07/hr commit (7-day lookback) showed:
- Utilization: 99% (commitment essentially fully used)
- Coverage: 88% (12% of c7i usage spills to on-demand at peaks)

The analyzer issued a "higher than recommended" warning against the
30-day window. That window included the pre-`qf1` period (qf1 launched
2026-05-18) when c7i usage was lower. Going forward `qf1` is permanent,
so the 7-day window is the representative one. Proceeded at $1.07/hr.

### Deferred: r6i EC2 Instance Savings Plan

- n1 (r6i.4xlarge) only launched 2026-06-09; Cost Explorer hasn't
  ingested enough usage to run a meaningful Purchase Analyzer yet.
- Revisit in 2-3 days (~2026-06-12 to 06-13).
- Plan: EC2 Instance SP, r6i family, Tokyo, 1yr, No-upfront,
  start at **~$0.92/hr** in the analyzer and adjust based on actual
  discount % (likely different from c7i's 23%).
- The earlier doc-wide caveat "don't buy a SP until n1 has run ~2 weeks"
  was about not locking the **r6i** family early — it still applies to
  the r6i purchase, but did not block the c7i purchase since the two
  SPs are independent.

### Key parameter decisions (for future SP purchases)

- **EC2 Instance SP over Compute SP** — chose +10pp deeper discount over
  family/region flexibility we don't need (c7i + r6i in Tokyo both
  stable).
- **1-year over 3-year** — still validating fleet shape. Revisit 3-year
  in ~12 months. Cost of waiting: ~10-15pp of additional discount.
- **No-upfront over All-upfront** — simplicity over the ~5-10pp extra
  discount from paying cash upfront.
- **Commitment-sizing rule: commit to the floor, not the ceiling.** No
  growth headroom in the commitment. SPs stack with no penalty — buy
  another SP when a new box actually arrives. Asymmetric risk: a new
  box running on-demand for a few weeks is cheap; unused commitment
  bills every hour for 12 months.

### Net effect on monthly bill

| Line | Before | After c7i SP | After both SPs (projected) |
|---|---|---|---|
| c7i fleet (5×xlarge + 1×large) | ~$840 | ~$565 (saves $276) | ~$565 |
| r6i.4xlarge (n1) | ~$780 | ~$780 | ~$540 (projected, ~30% off) |
| Other (split-box, EBS, EIPs) | ~$200 | ~$200 | ~$200 |
| **Total** | **~$1,820** | **~$1,544** | **~$1,304** |

(Projections are rough — actual r6i discount % will replace the
estimate once the r6i analyzer runs.)

## Notes for the cost-review agent

- **Don't terminate anything without checking with David.** The stopped
  instances might be intentional cold-storage. The `MYIP1` EIP is
  particularly suspicious — it has a deliberate name and points at a
  named-but-stopped instance. Probably reserved-on-purpose.
- **Don't buy any Savings Plan until n1 has run ~2 weeks** in its
  current shape — locking the r6i family for 1-3 years before we're
  sure of the spec is the kind of mistake that's painful to unwind.
- **All trading boxes are in Tokyo for HL latency.** Cross-region moves
  are off-limits.
- The dev workstation (this box) and any other non-AWS systems aren't
  in scope.
