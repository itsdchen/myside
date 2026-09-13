# Fleet-Wide Inheritance-Attributed Review — 2026-07-15

First systematic pass of PnL attribution across the full deploy fleet. Motivated
by two observations that made raw PnL misleading:

1. Many strats **inherit positions from siblings** through the `global_positioner`
   / `TradeRiskMan` handoff (see `trade_risk_man.cc:1567`
   `(SYM) Inherited position (QTY). New position: QTY`). The receiver's PnL is
   partly driven by market drift on positions they didn't open.
2. We recently fixed the wsgateway cancel throughput bug (`e18e4552`, merged to
   main via PR #1023 on 2026-07-09). Pre-fix trading is not representative of
   current behaviour, so the analysis window is **2026-07-01 → 2026-07-14**
   (10 trading days).

Tooling: `overmind/strat_main/tools/trademan/fleet_review.py`.

## Attribution method — precise definition

For each `(alias, group, strat, sym, date)` with trades:

```
SOD_pos      = first_trade.end_pos − signed_size(first_trade)   # from trades CSV
first_mid    = first_trade.mid  (fallback: first_trade.price)
last_mid     = last_trade.mid   (fallback: last_trade.price)
inherit_mtm  = SOD_pos × (last_mid − first_mid)
own          = net_pnl − inherit_mtm
```

**What `inherit_mtm` measures, exactly**:

The change in mark-to-market value of the inherited SOD position **if it had
been held constant** across the strat's session — marked at `first_mid` at
open and `last_mid` at close. It is a **fair-value benchmark**, not a
strategy attribution: it does not depend on what the strat actually did, only
on the SOD position and the two market mids.

**What `own` (strat_own) measures, exactly**:

The difference between what the strat *actually realized* and what a pure
buy-and-mark-to-market on the SOD position would have shown. Captures three
effects mixed together:

1. **Spread capture** from making markets (MM captures ~half-spread on each
   round trip → positive contribution)
2. **Adverse selection** on fills (someone hits our bid then the market
   moves away → negative contribution)
3. **Positioning changes via signal** beyond the inherited SOD (either sign,
   depends on signal quality that session)

**Sign interpretation**:

- `own > 0` — strat's active trading ADDED value beyond the buy-and-hold
  mark. Spread capture net of adverse selection was positive.
- `own < 0` — strat's trading SUBTRACTED value vs the buy-and-hold mark.
  **This does NOT mean the strat lost money.** `net_pnl` can still be
  strongly positive. On days when the SOD position drifted favorably, a
  well-functioning MM will often show `own < 0` because the pure
  buy-and-hold benchmark also earned that drift and the MM's
  flatten-by-EOD activity gave some of it back.

**What `own` does NOT measure**:

- **The strat's edge.** `own < 0` on any single window is not a disable
  trigger.
- **A verdict on any pair.** That's the job of the kill decision framework
  (`overmind/studies/kill_decision_framework.md` — forward monthly hit rate
  and PnL trajectory).

The classification categories emitted by `cmd_classify`
(FAKE_WINNER, REAL_BLEEDER, OVERACHIEVER, etc.) are **diagnostic labels for
the sign pattern of (net, inh_mtm, own)** — never edge verdicts. Always
cross-check with the framework before recommending action. Two of today's
mistakes (GOOGL and 3× AAPL) came from treating a diagnostic label as an
edge verdict.

- **Cross-family strats (`RelCross`) end flat by design.** SOD_pos = 0
  always, so `inherit_mtm = 0` and `own = net_pnl`. All Cross PnL rolls
  straight through.

### Known limitations

- Mid-day inherit events (a second GPCalc handoff during the session) get
  absorbed into `strat_own`. For strats with material intraday handoffs this
  understates inheritance's share. NVDA on `usday` gets a second inherit at
  ~13:36 daily; that piece isn't attributed.
- Zero-inheritance days (`SOD_pos == 0`) attribute 100% of PnL to `strat_own`
  (correct), so the "clean" comparison days are visible via the `n_inh` column.
- The last-trade mid is a proxy for the "close-out price". If the strat exits
  its inherited position early in the session and then does independent
  trading, the `inherit_mtm` proxy will be noisier. A future refinement is
  FIFO attribution using cumulative signed size.

## What the fleet looks like

Fleet total over the 10-day window, across **67 (alias, group, strat)**
instances trading **2,024 sym-day rows**:

| metric | value |
|---|---:|
| net PnL | **+$53,848** |
| inheritance MTM contribution | +$4,132 (+8%) |
| strat's own contribution | +$49,717 (92%) |

At the top level, the fleet is genuinely earning edge — inheritance is a small
contributor. The interesting story is in the composition.

### Per-family read

Sorted by strat_own:

| family | strats | sym-days | net | inh | own | inh_share |
|---|---:|---:|---:|---:|---:|---:|
| usday (RelWideMM2 day-session eq) | 4 | 503 | +$15,983 | +$3,315 | +$12,669 | 21% |
| cross (RelCross, all venues) | 12 | 419 | +$13,054 | $0 | +$13,054 | 0% |
| foreign_cov | 4 | 60 | +$12,200 | −$381 | +$12,580 | −3% |
| foreign_session (jp225, hkstocks, japan am/pm) | 3 | 60 | +$10,776 | +$39 | +$10,737 | 0% |
| cme_search (david_newsearch) | 2 | 136 | +$1,826 | +$314 | +$1,512 | 17% |
| postusa | 1 | 159 | +$465 | −$8 | +$473 | −2% |
| cme_single (gbp, gold) | 2 | 16 | +$379 | −$71 | +$450 | −19% |
| cme_allday_cov | 2 | 19 | +$243 | −$36 | +$279 | −15% |
| **boats/rel_nq** | 10 | 298 | +$328 | +$1,195 | **−$867** | 365% |
| **other (weekend_all)** | 2 | 248 | −$400 | +$460 | **−$860** | −115% |
| allday (allday_0706, new) | 2 | 98 | −$499 | −$621 | +$122 | 124% |
| new_0706 | 2 | 8 | −$507 | −$75 | −$432 | 15% |

Key reads:

- **Cross is all genuine** (0% inheritance by design). Korea `cross_0527` +
  `cross_0616` alone are +$10,057.
- **usday is genuinely profitable**, but composition is misleading — see below.
- **boats/rel_nq is a net loser on its own edge**, propped up by inheritance
  windfalls. Every individual boats variant with positive net has smaller (or
  negative) strat_own.
- **gf3 carries the fleet.** gf1/gf2 equities are heavily inheritance-cushioned.
  The gf1/gf2 `usday_cov_0521` and `usday_cov_0616` variants have essentially
  zero-to-negative own edge; their net PnL is inheritance-flow.

## Actionable classification

Each `(alias, group, strat, sym)` pair with ≥3 days is bucketed:

| category | rule (approximate) | reading |
|---|---|---|
| FAKE_WINNER | net>0 AND own<-100 AND |inh|>|own| | net looks fine, edge is bad — dangerous |
| REAL_BLEEDER | own<-200 AND net<0 | visibly losing, own bad |
| CUSHIONED_BLEEDER | own<-100 AND net<-50 AND inh>50 | net loss cushioned by good inheritance |
| MARGINAL_BLEEDER | own<-50 AND |net|<100 | quiet steady bleed |
| OVERACHIEVER | own>200 AND net>0 AND inh<-100 | net positive despite inheritance drag — real edge |
| GENUINE_WINNER | own>100 AND net>0 | real edge, no action |
| NEUTRAL | remainder | mostly noise, small samples |

Counts over the window:

| category | n pairs | net total | own total |
|---|---:|---:|---:|
| FAKE_WINNER | 9 | +$5,599 | −$3,733 |
| REAL_BLEEDER | 25 | −$15,294 | −$16,363 |
| CUSHIONED_BLEEDER | 2 | −$219 | −$352 |
| MARGINAL_BLEEDER | 14 | −$703 | −$988 |
| OVERACHIEVER | 19 | +$17,781 | +$27,597 |
| GENUINE_WINNER | 58 | +$47,194 | +$45,159 |
| NEUTRAL | 142 | +$444 | −$987 |

Actionable set: **50 bleeder/fake-winner pairs** to reduce/cut/investigate,
plus **19 overachievers** that may warrant sizing up.

**Potential PnL swing** if all bleeders were cleanly removed = +$21,436 (own)
over the 10-day window ≈ **+$2,144/day**. Sizing overachievers 2× would add up
to another ~$27k/window. Real recoverable is less because "cutting" a strat
doesn't fully zero its inheritance flow — but the order of magnitude is right.

## Verdict from full-history overlay

For each actionable pair, `fleet_review.py history` also pulls **lifetime
history** and assigns a `verdict`:

| verdict | meaning |
|---|---|
| ALWAYS_BAD | never profitable, or negative avg over lifetime |
| OLD_DECAY | peak was >20 trading days ago, sustained drawdown since |
| RECENT_DECAY | peaked in last 20 trading days, meaningful drawdown |
| CYCLICAL | oscillating, no clear trend |
| STEADY_EARNER | positive in each recent month |
| NEW/THIN | <15 trading days of history |

Category × verdict crosstab (this run):

| verdict → | ALWAYS_BAD | OLD_DECAY | RECENT_DECAY | CYCLICAL | STEADY | NEW/THIN |
|---|---:|---:|---:|---:|---:|---:|
| FAKE_WINNER | 0 | 2 | 5 | 2 | 0 | 0 |
| REAL_BLEEDER | 7 | 2 | 13 | 1 | 0 | 2 |
| CUSHIONED_BLEEDER | 1 | 0 | 1 | 0 | 0 | 0 |
| MARGINAL_BLEEDER | 1 | 2 | 5 | 0 | 0 | 6 |
| OVERACHIEVER | 0 | 1 | 8 | 4 | 5 | 1 |

### Clean kills (9 ALWAYS_BAD pairs)

Never worked; no history to save.

| pair | lifetime | days |
|---|---:|---:|
| `gf2/usday_cov_0521 xyz:EWY` | −$2,097 | 33 |
| `gf2/usday_cov_0521 xyz:MU` | −$1,010 | 33 |
| `gf2/usday_cov_0616 xyz:BIRD` | −$683 | 17 |
| `gf1/usday_cov_0616 xyz:NVDA` | −$584 | 18 |
| `gf2/gf2_rel_nq_2_boats xyz:MSFT` | −$432 | 20 |
| `gf2/gf2_rel_nq_allday_boats xyz:MSFT` | −$415 | 20 |
| `gf2/gf2_rel_nq_narrow_1_boats xyz:MSFT` | −$315 | 18 |
| `gf1/eq_gf1_cross v3 xyz:NVDA` | −$263 | 20 |
| `gf2/eq_gf2_cross v2 xyz:DKNG` | −$124 | 20 |

### Recent decay — postmortem candidates (5 headline cases)

Historically valuable, currently breaking. Order by expected value of
diagnosing:

| pair | lifetime | peak_dt | dd since | Feb → Jul monthly avg |
|---|---:|---:|---:|---|
| `gf1/usday xyz:NVDA` | +$27,044 | 06-25 | −$5,717 | +610 → +406 → +410 → +81 → −441 |
| `gf3/cross_0527 xyz:SMSN` | +$4,077 | 06-28 | −$748 | 0 → 0 → 0 → +200 → −83 |
| `gf3/cross/v3 xyz:QNT` | +$1,024 | 07-01 | −$734 | 0 → 0 → 0 → +144 → −42 |
| `gf2/usday_cov_0521 xyz:INTC` | −$605 | 06-25 | −$1,373 | 0 → 0 → +56 → −10 → −51 |
| `gf2/usday_cov_0521 xyz:MSFT` | −$94 | 06-25 | −$1,766 | 0 → 0 → +63 → +44 → −114 |

NVDA on `gf1/usday` is the standout — the decay started **in June**, before
the fast-cancel fix landed. Signal decay / regime change, not the cancel bug.
Same date-clustering: NVDA/INTC/MSFT all peaked on **2026-06-25**, suggesting
a market-level tech-mega-cap regime change worth checking against topbook /
HL micro-structure (per the postmortem framework in
`usday_symbol_degradation_observations_20260618.md`).

### Fake winners (positive PnL, negative own edge)

Most dangerous — raw PnL says "keep running":

| pair | net | own | inh |
|---|---:|---:|---:|
| `gf1/usday xyz:GOOGL` | +$2,605 | **−$1,090** | +$3,696 |
| `gf2/usday_cov_0521 xyz:AAPL` | +$812 | −$217 | +$1,029 |
| `gf2/usday_cov_0616 xyz:AAPL` | +$293 | −$1,010 | +$1,303 |
| `gf3/usday xyz:GME` | +$201 | −$392 | +$593 |
| `gf3/cov_0330 xyz:SKHX` | +$1,417 | −$234 | +$1,651 |

Note SKHX's lifetime is +$55k — the negative own edge this window is likely a
variance blip on top of a huge earner. The others are more concerning; AAPL
fails as fake-winner on both usday_cov_0521 and usday_cov_0616.

### Steady earners for size-up consideration

| pair | lifetime | jul monthly | notes |
|---|---:|---:|---|
| `gf3/hkstocks/am xyz:ZHIPU` | +$8,536 | +$456/d | biggest OT |
| `gf3/usday xyz:DELL` | +$1,533 | +$117/d | accelerating |
| `gf3/usday xyz:DRAM` | +$11,205 | +$109/d | stable large earner |
| `gf0/david_newsearch_jun2 xyz:GOLD` | +$1,048 | +$30/d | stable |
| `gf0/david_newsearch_latejun xyz:GOLD` | +$665 | +$24/d | new, growing |

Capacity sweep methodology from
`allsym_relcross_capacity_20260531.md` applies — check per-sym efficiency %
before scaling.

## Cross-cutting observations

1. **06-25 tech mega-cap regime change** — NVDA, INTC, MSFT all peaked exactly
   on 2026-06-25 on RelWideMM2 variants. Suggests a shared cause (either
   market regime or a shared config change). One postmortem may cover all
   three.
2. **MSFT is universally bad on RelWideMM2 equities** — fails on 4
   independent strat variants. Universal sym-off candidate.
3. **NVDA is strat-specific** — great on `gf1/usday` historically (+$27k),
   losing on `gf1/usday_cov_0616`, `eq_gf1_cross/v3`. The sym–strat pairing
   matters more than the sym alone.
4. **SMSN loses on both cross variants** (`cross_0527`, `cross_0616`) — SMSN
   may just not be cross-tradable in the current mid/small-cap KOR window.
5. **AAPL is chronically a fake winner** on `usday_cov_0521` AND
   `usday_cov_0616` — the RelWideMM2 usday-cov family may not have edge on
   AAPL; it's the inheritance flow keeping it "positive".
6. **The `boats/rel_nq` family is systemically inheritance-dependent** — 10
   strats, collective own edge −$867, cushioned to +$328 by inheritance. If
   sibling flow shifts, the family goes negative.
7. **32 pairs are RECENT_DECAY** — a lot of things weakened around 06-25 to
   07-01. Some is small-sample noise; the clustered dates suggest a real
   underlying event.

## Artifacts

- Tool: `overmind/strat_main/tools/trademan/fleet_review.py`
- Attribution CSV: `~/scratch/tradeperf/_attribution_20260701.csv`
- Actionable CSV:  `~/scratch/tradeperf/_actionable_20260701.csv`
- History review CSV: `~/scratch/tradeperf/_history_review_20260715.csv`

## Follow-ups

- [x] **Applied 2026-07-15**: 9 ALWAYS_BAD pairs disabled in live configs
      (`enabled: false`). Backups on each host at
      `/tmp/{fname}.pre_kills.20260715`. **No manual restart** — pktrade
      processes pick up config on the next day's startup. Root-cause reads per
      symbol are captured in the "Root causes for the 9 kills" section below.
- [x] **Applied 2026-07-15 (second batch)**: 3 tech-cluster kills after
      the postmortem — NVDA on `gf1/usday`, INTC + MSFT on
      `gf2/usday_cov_0521`. Backups `/tmp/{fname}.pre_kills.20260715_r2`.
- [x] **Applied 2026-07-15 (third batch)**: 3 AAPL kills across
      `usday_cov_0521`, `usday_cov_0616`, `gf2_rel_nq_narrow_1_boats`.
      Same "professional maker arrival on wider spread + deeper inside" HL
      fingerprint as MSFT. Backups `/tmp/{fname}.pre_kills.20260715_r3`.
- [x] **Applied 2026-07-15 (fourth batch)**: GOOGL on `gf1/usday`
      disabled — showed the NVDA-like "quieter + thinner" HL fingerprint
      on 06-25 and had own_pnl −$1,090. Backup
      `/tmp/pk_usday.json.pre_kills.20260715_r4`.
- [x] **Applied 2026-07-15 (eighth batch — framework-audited kills)**:
      after building `kill_decision_framework.md` from the GOOGL/AAPL
      corrections, ran the framework sweep across the full actionable
      list. Of 12 substantial disable candidates the fleet review
      initially surfaced, framework properly applied said: **5 clear
      disables, 1 size cut, 4 HOLD/WATCH (cyclical or improving), 2
      too-thin**. Applied only the framework-confirmed subset:
      - **DISABLES**:
        - `gf3/combined_equities_bfx3/usday_cov_0516 xyz:LITE`
          (May 89%/+$1,560 → Jun 43% → Jul 50%/-$119; life -$656)
        - `gf3/combined_equities_bfx3/rel_nq_cov_0516_boats xyz:LITE`
          (Jun 64%/+$133 → Jul 20%/-$441)
        - `gf3/cross/v3 xyz:BB`
          (Jun 70% → Jul 30%/-$277; last 5d 20% hit/-$352)
        - `gf2/eq_gf2_cross/v1 xyz:MU`
          (Jul 0% hit rate/-$226 across 4 sessions)
        - `gf2/eq_gf2_cross/v1 xyz:NFLX` (disabled in both v3 and v4
          config files; last 5 days 0% hit rate/-$251)
      - **SIZE CUT**: `gf2/eq_gf2_cross/v2 xyz:ORCL` size_mult 10 → 5
        (June was big +$442 then July -$268; life still +$174 with
        ~60% drawdown from peak — cut but don't kill).
      - **HELD** (would have been wrong to kill): RIVN on 3 btc/boats
        variants (cyclical monthly pattern; last 10d bounces),
        DRAM on postusa (tiny losses ~$20-30/d), 2 weekend_all
        entries too-thin (6 sessions each).
      - Backups on remote at `/tmp/{fname}.pre_framework_v2.20260715`.
- [x] **Applied 2026-07-21 (framework audit with 4 more days of live data)**:
      re-ran fleet_review + framework audit on 07-21 data. 6 clean disables +
      1 size-up applied:
      - **Disables**: `gf1/allday_0706 xyz:BABA` (17% hit / −$915), 
        `gf3/allday_0706 xyz:MRVL` (**0% hit** rate all July), 
        `gf1/gf1_rel_nq_allday_boats xyz:AMZN` (Jun 50% → Jul 21%, −$929),
        `gf2/eq_gf2_cross/v2 xyz:HIMS` (Jun +$633 → Jul −$401),
        `gf0/david_newsearch_jun2 xyz:SP500` (both months negative),
        `gf2/allday_0706 xyz:BIRD` (Jul only, 33% hit, one winning day of 6).
      - **Size-up**: `gf3/hkstocks/pm xyz:ZHIPU` size_mult 1 → 3. Matches
        the 07-15 change on the `am` variant. Jul was best month
        (87% hit / +$2,046). Note: on the `am` variant post-07-15, PnL only
        rose +11% (456→506/d) for a 3× size increase — clear edge
        degradation at scale, so no reason to go past 3× on either variant.
      - **Manual overrides on classifier false positives** (framework HOLD,
        automated said DISABLE): TSM/usday_cov_0521 (Jun −$457 → Jul +$47,
        recovering); CL/jun2 (Jun −$154 → Jul −$79, less bad); EWT/allday_0706
        (too thin at 6 sessions). Held all three.
      - **Framework classifier bug noted**: the automated code returns
        DISABLE when `lifetime ≤ 0`, even when Layer 1 clearly shows
        recovery. TSM was the clearest example. `fleet_review.py` should
        be patched to let Layer 1 (improving trajectory) override
        Layer 2 lifetime-check. Follow-up.
      - Backups on remote at `/tmp/{fname}.pre_0721_action.20260721`.
- [x] **Correction 2026-07-15 — AAPL RE-ENABLED (all 3 variants)**:
      after the GOOGL re-enable, retroactive framework check on all
      non-ALWAYS_BAD disables revealed AAPL on all 3 variants was killed
      based on backward attribution + fill_bp z-spike, but forward
      monthly hit rate + PnL all supported HOLD:
      - `gf2/combined_equities_bfx2/usday_cov_0521 xyz:AAPL`: monthly
        hit rate **50%→67%→82%** (Jul is best month ever, +$1,149);
        lifetime **+$1,942** with peak = TODAY (0% drawdown).
      - `gf2/combined_equities_bfx2/usday_cov_0616 xyz:AAPL`: Jun 88%
        → Jul 64% (hit rate declining but Jul PnL up +$320 vs +$124);
        lifetime +$444, drawdown -19%. Framework HOLD.
      - `gf2/combined_equities_bfx2/gf2_rel_nq_narrow_1_boats xyz:AAPL`:
        Jul improving (last 5d 60%/+$117); tiny sample.
      Backups at `/tmp/{fname}.pre_aapl_reenable.20260715`. Same class
      of error as GOOGL; framework applied correctly next time.
- [x] **Correction 2026-07-15 — GOOGL RE-ENABLED**: GOOGL on
      `gf1/combined_equities_bfx1/usday` was disabled Round 4 based on
      FAKE_WINNER attribution (own −$1,090 via MTM proxy) and Group B
      HL fingerprint. Hit-rate check after META lesson revealed:
      **July was GOOGL's best month in 105 sessions** (80% hit rate,
      +$1,754 monthly net). Disable was based on backward attribution
      proxy, not forward-looking PnL trajectory. Re-enabled 2026-07-15.
      Backup at `/tmp/pk_usday.json.pre_googl_reenable.20260715`.
      **Lesson recorded** in `overmind/studies/kill_decision_framework.md`
      (new). The framework requires forward monthly hit rate + PnL
      check before any disable on a currently-earning strat.
- [x] **Applied 2026-07-15 (seventh batch — QNT size cut)**: QNT on
      `gf3/cross/v3` size_mult 8 → 4 in `pk_cx_gf3_1.json`. Ran up to
      peak +$1,758 on 07-01 then two −$600 gap-down days (07-02 and
      07-15) wiped 77% of the peak. Not a clean regime break — HL
      market got quieter/thinner (opposite of tech-cluster pattern),
      likely fast-directional-move risk on Korean-hours book. Half cut
      preserves upside; full kill if another −$400+ day comes soon.
      Backup `/tmp/pk_cx_gf3_1.json.pre_qnt_cut.20260715`.
- [x] **Applied 2026-07-15 (sixth batch — offense/sizing-up)**: capacity
      analysis on the 5 STEADY_EARNERS led to 3 size-ups:
      - **ZHIPU** on `gf3/hkstocks/am` — `size_mult 1 → 3`. Config
        `pk_hkex_am.json`. Peak abs open_pos was only 53.7 vs
        ordex.max_pos=20000; win-to-loss asymmetry ~20:1. Expected
        upside ~$800/d avg if performance holds.
      - **DELL** on `gf3/combined_equities_bfx3/usday` — initially
        (mistakenly) raised `max_position 62 → 100` and `ordex.max_pos
        48 → 80` in `pk_usday.json`. **Corrected 2026-07-15**: reverted
        both caps back to original (62 / 48) and raised `size_mult
        10 → 15` instead. Rationale: `size_mult` is the intended sizing
        knob; `max_position` / `ordex.max_pos` are safety limits, not
        sizing targets. The correct way to size up DELL is via
        size_mult; the peak-of-50 vs cap-of-48 observation was the
        safety net doing its job, not a signal that caps should
        be loosened.
      - **GOLD** on `gf0/combined_exotic_gf0/david_newsearch_latejun` —
        `size_mult 5 → 10` in `pk_fullsearch_0624.json`. Aligns with the
        jun2 variant sizing (which is at 15) on the same symbol.
      - **DRAM** (hold — post-fix soft: May +$473/d → Jul +$77/d).
      - **GOLD/jun2** (hold — already at reasonable size).
      - Backups on remote at `/tmp/{fname}.pre_size_up.20260715`.
- [x] **Applied 2026-07-15 (fifth batch)**: SMSN treated as a
      partial-break (mild HL micro-structure shift, big-vol-day upside
      still present) rather than a clean kill.
      - SMSN on `gf3/combined_foreign_gf3/cross_0616` **disabled** —
        was at size_mult=5 with lifetime +$355 (essentially breakeven
        per unit size), 36% loss days, dominated by same HL fingerprint
        as 0527 but 10× the exposure.
      - SMSN on `gf3/combined_foreign_gf3/cross_0527` **size cut
        from 0.5 → 0.25** in `gf3_kor_relcross_v3.json` — preserves
        upside on the next big-vol day (like 06-23 +$1,132) while
        halving the drawdown drag from the tighter/denser HL book.
      - Backups `/tmp/pk_cx_gf3_kor_1.json.pre_kills.20260715_r5` and
        `/tmp/gf3_kor_relcross_v3.json.pre_size_cut.20260715`.
- [x] **Post-kill investigation 2026-07-15**: what happened on 2026-06-25.
      Micron beat earnings after close 06-24; AI-chip complex rallied 06-25
      (NVDA/AMD/INTC +1.4-5% premarket); Apple + Microsoft announced product
      price hikes same day; MAG7 fell on the news. Cross-scan of tech
      mega-caps confirms 6 of 7 (all but LLY) show the 06-25 HL fingerprint
      in one of two shapes: (a) MSFT/AAPL/AVGO/META/AMZN/ORCL — deeper inside
      + wider spread + larger trades ("new maker arrived"); (b)
      NVDA/INTC/GOOGL/TSLA/TSM — quieter book + thinner inside + wider
      spread ("makers left"). Full per-symbol writeup in postmortem
      `pm_techmm_20260625break_20260715.md` and `symbol_regime_log.md`.
- [x] **Older postmortem corrections 2026-07-15**: `pm_mrvl_usday_20260618.md`
      and `pm_ewy_usday_cov_0313_20260618.md` were re-run with
      `pm_hl_stats.py --weekdays-only`. Both used calendar-day aggregation
      originally, which included weekend HL activity and inflated ratios.
      Corrections annotated inline. MRVL notional was 26× not 1,045×;
      inside_liq was 1.95× deeper not 0.69× thinner (direction flipped).
      EWY-0313 notional was 0.81× *lower* not 3.6× higher (direction
      flipped). Qualitative verdicts survive; magnitudes were wrong.
- [ ] Postmortem: **tech mega-cap cluster peaking exactly on 2026-06-25**:
      NVDA on `gf1/combined_equities_bfx1/usday`, INTC + MSFT on
      `gf2/combined_equities_bfx2/usday_cov_0521`. Three symbols, two
      different RelWideMM2 variants, same peak date → cause is market-side
      (regime shift + HL participant change), not strat-config.
- [ ] Postmortem: SMSN on cross_0527/cross_0616 — cross variant selection
- [ ] Decision: AAPL sym-off across cov_0521 and cov_0616
- [ ] Decision: MSFT sym-off across the remaining RelWideMM2 variant (`usday_cov_0521`)
- [ ] Capacity sweep on the 5 STEADY_EARNERS
- [ ] Extend attribution to use cost-basis from GPCalc log lines rather than
      MTM proxy (would sharpen fake-winner / overachiever classification)
- [ ] Consider adding `fleet_review run` to the cron alongside the existing
      `pta scan --days 1` — weekly, `--since` set to the fast-cancel-fix date

## Root causes for the 9 kills

Investigated with `pm_hl_stats.py` + strat-side acct rollups. Each kill has a
distinct HL micro-structure fingerprint but all in the **2026-06-22 → 06-28
window** — same week as the tech-cluster peak on `usday_cov_0521`
(NVDA/INTC/MSFT) and the US-equities cancel-RTT degradation start. The July
fast-cancel fix restored throughput but couldn't restore edge because the
underlying HL markets changed.

| sym / strat | inflection | HL fingerprint (bad/base ratios) | reading |
|---|---|---|---|
| **EWY** on gf2/usday_cov_0521 | 06-25/26 (−$715/−$999) | trades 2.14×, notional/day 2.49×, inside_liq 2.99×, mid_changes/day 2.10× | New HL participants — **deeper, faster book**. Opposite direction from the 0313 EWY postmortem (thinning) but same phenomenon: participant regime change. Our edge disappeared. |
| **MU** on gf2/usday_cov_0521 | 06-23 accel | trades 2.45×, notional/day 3.44×, spread 1.53× **WIDER**, avg_mid +17%, med_trdsz 0.53× | MU rallied with tech complex; activity 2-3× but spreads widened — transitional/volatile regime our MM strat wasn't sized for. |
| **BIRD** on gf2/usday_cov_0616 | 06-29 peak → 07-07 crash | trades **0.21×**, notional/day **0.093×**, price **−30%**, wider spread | Underlying interest evaporated (10× less notional). Strat can't run in a dead market. Was already disabled by the time the fleet review re-ran. |
| **NVDA** on gf1/usday_cov_0616 | 06-24 peak → 06-26 break | trades 0.36×, vol 0.34×, **med_inside_liq 0.011× (99% thinner)**, wider spread | HL takers left NVDA; book got extremely thin. Massive adverse-selection risk when quoting into a thin book. |
| **MSFT** on 3× boats variants | **06-28 (all three same date)** | trades 0.75×, vol 0.73×, **med_inside_liq 13.26×**, med_trdsz 2.00×, tighter spread | Aggressive HL maker arrived on MSFT — 13× denser inside + 2× larger trades. Our boats variants can't fade a passive-hedged flow. Breaking on the same date across 3 configs pins it to the underlying HL market. |
| **NVDA** on gf1/eq_gf1_cross/v3 | never scaled | (shared with NVDA reading above; 99% inside thinning) | Cross variant needs directional signal; thin book kills the fill rate at the entry price. |
| **DKNG** on gf2/eq_gf2_cross/v2 | never worked | trades 1.19×, inside_liq 3.94×, mid_changes 2.47× | DKNG HL got busier and denser — same "new participants" fingerprint. Cross needs exploitable staleness; the new market has none. |

**Common thread**: the HL micro-structure changed materially for every one of
these symbols in the 06-22 → 06-28 window. Different directions per symbol
(some thinner, some denser, one dried up), but the underlying event was
shared. This clustering is worth its own investigation — could be an HL
platform change (fee schedule, matching engine, onboarding), concentrated
arrival of institutional makers/takers, or knock-on effects of the tech-mega-cap
rally that ran in the same window.
