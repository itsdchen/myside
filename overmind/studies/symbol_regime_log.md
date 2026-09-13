# Symbol Regime Log

Registry of symbols where market micro-structure has **fundamentally changed**
in a way that invalidates our prior tuning. Append-only per-symbol entries,
dated when the change was observed and cross-referenced to the underlying
postmortem(s).

Purpose: prevent re-deploying (or continuing to deploy) a symbol whose
market has structurally shifted, without a fresh baseline. If you see a
symbol here and are considering redeploy, first re-baseline with fresh
sim/live data — the prior configs and tuning assumed conditions that no
longer hold.

## How to use

- **Before deploying a symbol** (new strat, retune, cov variant), check this
  log. If listed, treat as needing a fresh baseline; do not size on
  historical PnL.
- **When authoring a postmortem** with a `regime` verdict, add an entry
  here. Cross-reference back to the postmortem for evidence.
- **When a symbol appears to have recovered**, don't delete the entry —
  add a follow-up dated entry noting the observation and the new baseline
  window.

## Entry format

```markdown
### `xyz:SYM` — {short label of change} — YYYY-MM-DD

- **Observed on strats:** `alias/group/strat` (comma-separated if multiple)
- **Inflection date:** YYYY-MM-DD (session where the change first manifested)
- **What changed:** one-paragraph micro-structure fingerprint
- **HL vs topbook:** brief note on divergence direction
- **Impact on our strats:** what broke and how
- **Current status:** ACTIVE / DISABLED / RETUNED / RECOVERED
- **Related postmortem(s):** links
- **Baseline for redeploy:** what window to use if re-baselining
```

---

## Entries

### `xyz:EWY` — HL participant regime shift (multiple episodes) — 2026-06-18, 2026-06-25

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0313`
  (episode 1), `gf2/combined_equities_bfx2/usday_cov_0521` (episode 2)
- **Inflection date:** 2026-06-11 (usday_cov_0313 episode),
  2026-06-25 (usday_cov_0521 episode)
- **What changed (0313 episode)** — CORRECTED 2026-07-15 with weekday-only:
  HL notional/day **0.81× (dropped 19%)** (original doc said 3.6× higher —
  flipped direction!), num_trades **0.39× (61% fewer)** (original said 2.08×
  higher), n_midchanges/day **1.02× (flat)** (original said 4×),
  med_inside_liq 0.70× thinner (original 0.30× — direction confirmed but less
  extreme). Corrected story: **HL participants withdrew** (not "new aggressive
  participant arrived"). Real-world topbook still flat.
- **What changed (0521 episode)**: HL notional/day 2.5×, inside book 3.0×
  DEEPER (opposite direction from 0313), mid_changes 2.1×. Real-world
  topbook flat.
- **HL vs topbook:** both episodes are HL-only regime shifts; topbook flat
  in both cases. First episode's fingerprint was "thinning"; second's was
  "densification." Different HL participant behaviors → different signature,
  same underlying cause type.
- **Impact:** 0313 episode: −$807/day avg (−$2,984 over 4 days). 0521
  episode: −$807 → smaller losses that added up to −$2,097 lifetime.
  Different fingerprint but same story of "our MM edge disappeared."
- **Current status:** **DISABLED** on both variants (0313 removed
  by 2026-06-18; 0521 removed 2026-07-15).
- **Related postmortems:**
    - `postmortems/pm_ewy_usday_cov_0313_20260618.md`
    - `postmortems/pm_techmm_20260625break_20260715.md` (as part of the
      broader ALWAYS_BAD kill list)
- **Baseline for redeploy:** would need a fresh 3+ week HL window post any
  future stabilization. EWY has now had two participant regime shifts in
  ~2 months — this may be a name where our thin-market MM edge is
  structurally fragile.

### `xyz:MRVL` — underlying-driven HL discovery — 2026-05-29

- **Observed on strats:** `gf3/combined_equities_bfx3/usday`
- **Inflection date:** 2026-05-29
- **What changed** — CORRECTED 2026-07-15 with weekday-only: HL notional/day
  **26×** (original doc said 1,045×), num_trades **4.2×** (original 74×),
  n_midchanges/day **2.7×** (original 34.5×), spread 0.27× (original 0.13×),
  **med_inside_liq 1.95× DEEPER** (original 0.69× thinner — direction
  flipped), avg_mid +52%. Simultaneous with an ~80% underlying rally and
  1.65× topbook realized vol. Story of "MRVL got discovered" still holds
  but magnitudes were badly inflated by weekend contamination.
- **HL vs topbook:** both moved together — the rare "underlying event"
  case (vs the more common HL-only case).
- **Impact:** MRVL went from thin-edge earner to whipsaw victim; strat's
  own trades_per_day 3× and flips_per_day 8.5× before manual size cut on
  2026-06-15.
- **Current status:** **DISABLED**.
- **Related postmortems:**
    - `postmortems/pm_mrvl_usday_20260618.md`
- **Baseline for redeploy:** MRVL is now a "discovered" HL contract with
  vastly deeper flow. Prior thin-market edge is gone. If redeployed, treat
  as a completely different symbol — new signal/threshold search required.

### `xyz:NVDA` — HL divergence from topbook — 2026-06-25

- **Observed on strats:** `gf1/combined_equities_bfx1/usday`,
  `gf1/combined_equities_bfx1/usday_cov_0616`,
  `gf1/eq_gf1_cross/v3`
- **Inflection date:** 2026-06-25
- **What changed:** HL notional/day 0.61× (vs topbook 0.80×) — HL
  slowdown outpaced real-world. Inside book thinned 26%, spread widened
  10%. Cluster with INTC and MSFT on the same date.
- **HL vs topbook:** HL diverged (fell faster); real-world only mildly
  slowed.
- **Impact:** on `usday` (lifetime +$27k earner), monthly avg went
  +$610/d (Feb) → +$81/d (Jun) → −$441/d (Jul). Fill rate collapsed
  toward zero in the bad window. On `usday_cov_0616` and `eq_gf1_cross/v3`
  it was already marginal/never-profitable.
- **Current status:** **DISABLED** on all three (2026-07-15).
- **Related postmortems:**
    - `postmortems/pm_techmm_20260625break_20260715.md` (primary)
    - `studies/fleet_review_20260715.md` (context)
- **Baseline for redeploy:** wait for post-06-25 HL micro-structure to
  stabilize (2+ weeks flat inside_liq and mid_changes) before rebaselining.
  Consider whether the HL–topbook decoupling persists — that's the
  structural change to watch.

### `xyz:INTC` — HL divergence from topbook — 2026-06-25

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0521`
- **Inflection date:** 2026-06-25 (cluster with NVDA and MSFT)
- **What changed:** HL notional/day 0.63× (vs topbook 0.80×). Inside book
  and mid_changes essentially flat. Fewer, larger trades from a quieter
  HL market than the real-world.
- **HL vs topbook:** HL diverged (fell faster).
- **Impact:** hit rate 53% → 29%, avg PnL/d +$32 → −$87. Sent 1.70× more
  orders for the same fill rate (each fill less valuable). min_pnl 2.23×
  worse.
- **Current status:** **DISABLED** (2026-07-15).
- **Related postmortems:**
    - `postmortems/pm_techmm_20260625break_20260715.md`
- **Baseline for redeploy:** same guidance as NVDA — post-06-25 HL
  stabilization + fresh sim.

### `xyz:MSFT` — HL densification (professional maker arrival) — 2026-06-25

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0521`,
  `gf2/combined_equities_bfx2/gf2_rel_nq_2_boats`,
  `gf2/combined_equities_bfx2/gf2_rel_nq_allday_boats`,
  `gf2/combined_equities_bfx2/gf2_rel_nq_narrow_1_boats`
- **Inflection date:** 2026-06-25 (three boats variants broke on 2026-06-28,
  4 sessions later — same trigger event)
- **What changed:** HL notional/day 1.47× (topbook 1.01× flat). Inside
  book **2.46× deeper**, typical trade size 1.83× larger, spread 1.28×
  wider. Fewer transactions but each larger. Classic professional-maker
  signature.
- **HL vs topbook:** HL grew even as topbook stayed flat — a fresh HL
  participant arrived.
- **Impact:** shs_traded 2.30× (our resting orders getting picked off in
  larger sizes), fill_rate 0.39× (fewer orders survive to fill), min_pnl
  2.35× worse.
- **Current status:** **DISABLED** on all four variants (2026-07-15).
- **Related postmortems:**
    - `postmortems/pm_techmm_20260625break_20260715.md`
- **Baseline for redeploy:** hard to compete with a professional maker on
  their symbol. If we redeploy, need to be sure our edge is genuinely
  different (e.g., a signal they don't have) — otherwise we're adverse-selected
  by construction. Consider whether MSFT should be a permanent sym-off for
  RelWideMM2 equities.

### `xyz:BIRD` — underlying interest evaporated — 2026-06-29

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0616`
- **Inflection date:** 2026-06-29 (peak) → 2026-07-07 first crash
- **What changed:** HL notional/day **0.093× (10× less)**, HL num_trades
  0.21×, price −30%, ret_stdev 0.62×. Real-world topbook not yet checked
  but the HL fingerprint alone tells the story.
- **HL vs topbook:** likely both dropped; interest went away.
- **Impact:** the strat can't run in a dead market — fewer of our orders
  place, and each fill is a bigger relative move.
- **Current status:** **DISABLED** (was already disabled by 2026-07-15 —
  someone got there first).
- **Related postmortems:**
    - `studies/fleet_review_20260715.md` (root-cause section)
- **Baseline for redeploy:** don't. BIRD is a name whose HL interest
  ended. No structural reason to expect return.

### `xyz:MU` — tech rally regime shift — 2026-06-23

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0521`,
  `gf2/eq_gf2_cross/v1`, `gf2/eq_gf2_cross/v2`
- **Inflection date:** 2026-06-23 (loss acceleration)
- **What changed:** HL num_trades 2.45×, notional/day 3.44×, avg_mid
  **+17%**, spread **1.53× wider**, med_trdsz 0.53×. Activity + higher
  price + wider spread + smaller trade size — transitional/volatile
  regime. Not yet re-run with weekday-only filter (initial reading was
  calendar-day).
- **HL vs topbook:** not yet checked; expected to be underlying-driven
  given the ~17% price move.
- **Impact:** lifetime never profitable; loss accelerated post-06-23.
  Broken on 3 strat variants.
- **Current status:** **DISABLED** on all three (2026-07-15).
- **Related postmortems:**
    - `studies/fleet_review_20260715.md` (root-cause section)
- **Baseline for redeploy:** wait for MU price/vol to stabilize + fresh
  HL micro-structure baseline. MU appears to be a hard name for our MM
  during trending regimes.

### `xyz:DKNG` — HL densification — 2026-06-25

- **Observed on strats:** `gf2/eq_gf2_cross/v2`
- **Inflection date:** never really worked; slow drift
- **What changed:** HL num_trades 1.19×, vol_shs 2.5×, inside_liq 3.94×
  (denser), mid_changes 2.47×. Same "new participants" fingerprint as
  MSFT but on a smaller absolute scale. Not yet re-run weekday-only.
- **HL vs topbook:** not yet checked.
- **Impact:** never worked at scale; ~$120 lifetime bleed on ADD-based
  cross. Cross needs exploitable staleness; densified market has none.
- **Current status:** **DISABLED** (2026-07-15).
- **Related postmortems:**
    - `studies/fleet_review_20260715.md` (root-cause section)
- **Baseline for redeploy:** DKNG's HL is now a competitive market.
  Unlikely to yield to our cross variant without a signal edge.

### `xyz:META` — fill-rate spike ABSORBED (adaptation case) — 2026-06-25

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0521` and
  4 other variants (allday_boats, postusa, eq_gf2_cross v1/v2).
- **Inflection date:** 2026-06-25 (Group A "deeper inside + wider spread"
  fingerprint from the tech-cluster event; hl_divergence_monitor flagged
  META HIGH — notional_per_day z=+12.9, num_trades +8.3, med_trdsz +7.0).
- **What changed:** HL notional/day 3.89× larger, num_trades 1.38×,
  med_trdsz 2.36× — new maker arrival. Fill_bp z=+4.7 on
  `usday_cov_0521` (same magnitude as the MSFT/AAPL killers).
- **Impact — the surprise**: unlike MSFT/AAPL, META **adapted** and kept
  earning. `usday_cov_0521` monthly hit rate 48% (Jun) → **73%** (Jul);
  July net +$640 — the strat's best month. `postusa` recent 10-day hit
  rate 80%. Only `eq_gf2_cross/v1` shows recent softness (last 5 days
  20% hit rate, −$234) — worth watching but not acting.
- **Current status:** **HELD** on all instances (2026-07-15). Watch
  `eq_gf2_cross/v1` for 3-5 more sessions.
- **Related studies:** `studies/fleet_review_20260715.md`.
- **Calibration lesson**: fill_bp spike is a warning, not a verdict.
  Death happens only when fill_bp spike + hit rate collapse + PnL loss
  coincide. META had only the first — adaptation succeeded. Recorded
  in `hl_divergence_monitor.py` follow-up notes.

### `xyz:QNT` — elevated tail-day risk after strong run-up — 2026-07-02

- **Observed on strats:** `gf3/cross/v3`
- **Inflection date:** 2026-07-02 (first big loss day after peak)
- **What changed:** HL notional halved ($5-8M → $2.6M), med_inside_liq
  halved (70-96 → 40), spread widened modestly 13→16 bp. HL market got
  **quieter and thinner** — the opposite of the MSFT/AAPL "denser and
  wider" pattern. Fits a "cross strat's staleness edge dries up + fast
  directional move whipsaws IOC lifts" story.
- **HL vs topbook:** not checked (Korean-hours symbol).
- **Impact:** ran up 06-16 → 07-01 (peak +$1,758). Two "gap-down" days:
  2026-07-02 (−$593) and 2026-07-15 (−$615), each wiping ~⅓ of running
  total. Lifetime +$409 = **77% drawdown from peak**. Between the two
  loss days, mostly choppy but net-positive (+$304 on 07-13). Cross strat
  fill_bp dropped from 1,900-2,800 to 865-1,600 range — fewer stale-price
  opportunities.
- **Current status:** **SIZE CUT** — size_mult 8 → 4 on `gf3/cross/v3`
  (2026-07-15). Preserves some upside if next big-move day comes,
  halves tail-day damage. Similar treatment to SMSN (partial break, not
  clean kill), but SMSN had ~10× the buffer.
- **Related postmortems:** `studies/fleet_review_20260715.md`
- **Baseline for redeploy:** if another −$400+ loss day within 10
  sessions, likely full kill. If a clean +$300+ recovery day comes and
  the tail-day frequency drops, restore size to 6.

### `xyz:SMSN` — mild HL densification, big-vol upside preserved — 2026-06-29

- **Observed on strats:** `gf3/combined_foreign_gf3/cross_0527` (v3),
  `gf3/combined_foreign_gf3/cross_0616`
- **Inflection date:** 2026-06-29 (start of 6-day drawdown on 0527)
- **What changed:** HL spread tightened ~25% (5.2 → 3.8 bp),
  med_inside_liq deepened from ~4-5 to 16-20 by 07-06/07, notional/day
  ~2× higher. Same "professional maker arrival" fingerprint as MSFT/AAPL
  but delayed and more gradual. Also cross_0527 had extreme winners
  early in June: **06-23 was +$1,132 (28% of lifetime PnL)**, indicating
  SMSN's edge lives on big-vol days.
- **HL vs topbook:** not checked (Korean symbol; XNAS.BASIC doesn't cover it).
- **Impact:** cross_0527 peak +$4,825 on 06-28 → −$748 drawdown over 6
  days (still lifetime +$4,077). cross_0616 much more volatile,
  essentially breakeven (+$355) with 36% loss days and 10× sizing.
- **Current status:** **PARTIAL** — cross_0616 disabled (2026-07-15),
  cross_0527 size cut from 0.5 → 0.25 (2026-07-15). Preserves big-vol-day
  upside on cross_0527 while cutting drag.
- **Related studies:**
    - `studies/fleet_review_20260715.md`
- **Baseline for redeploy:** if cross_0527 gives back another $1,000 from
  peak, revisit — likely full kill. If a big-vol day returns +$500+ within
  ~2 weeks, consider restoring size_mult=0.5.

### `xyz:AAPL` — HL densification, but strat adapting — 2026-06-25

- **Observed on strats:** `gf2/combined_equities_bfx2/usday_cov_0521`,
  `gf2/combined_equities_bfx2/usday_cov_0616`,
  `gf2/combined_equities_bfx2/gf2_rel_nq_narrow_1_boats`
- **Inflection date:** 2026-06-25 (Apple iPhone price hike announcement)
- **What changed:** HL notional/day ~+80%, med_inside_liq **3-4× deeper**,
  spread widened ~50% (1.55-1.77 → 2.27-2.75 bp), price fell from ~$296
  to ~$282 on the news. Same day fill_bp doubled, cancel_fill% doubled.
- **HL vs topbook:** Apple stock down on price hike news; HL saw
  professional maker signature (deeper inside + wider spread + larger
  trades). Same-day event as the tech-cluster break.
- **Impact (corrected 2026-07-15)**: **strat adapted, not killed.**
  Was initially disabled based on backward attribution (FAKE_WINNER)
  + fill_bp z-spike. Retroactive hit-rate check via the new kill
  decision framework showed:
  - usday_cov_0521: monthly hit rate 50% → 67% → **82%** (Jul is
    best month ever, +$1,149). Peak PnL = TODAY (0% drawdown).
    Lifetime +$1,942.
  - usday_cov_0616: Jun 88% / +124 → Jul 64% / **+320** (hit rate
    down, PnL up). Framework: HOLD.
  - narrow_1_boats: last 5 sessions 60% / +$117 (recent improvement).
- **Current status:** **RE-ENABLED on all 3 variants (2026-07-15).**
  Same class of error as GOOGL — killed on backward flags, forward PnL
  said "adapting."
- **Related postmortems:**
    - `postmortems/pm_techmm_20260625break_20260715.md` (cluster)
    - `studies/fleet_review_20260715.md`
- **Baseline for redeploy:** wait for HL to stabilize post the Apple
  price-hike news cycle; fresh baseline required.

### `xyz:GOOGL` — HL divergence from topbook — 2026-06-25

- **Observed on strats:** `gf1/combined_equities_bfx1/usday`
- **Inflection date:** 2026-06-25 (part of tech-cluster event)
- **What changed:** HL num_trades **0.47×**, notional/day **0.62×**,
  spread 1.22× wider, med_inside_liq 0.83× (thinner). Same "quieter +
  thinner" NVDA/INTC-like pattern; makers left GOOGL on 06-25.
- **HL vs topbook:** HL activity fell harder than topbook (typical of
  the 06-25 tech mega-cap event).
- **Impact:** FAKE_WINNER — net +$2,605 masked by +$3,696 inheritance
  windfall; strat's **own edge −$1,090**.
- **Current status:** **DISABLED** on `gf1/usday` (2026-07-15).
- **Related postmortems:**
    - `postmortems/pm_techmm_20260625break_20260715.md`
    - `studies/fleet_review_20260715.md`
- **Baseline for redeploy:** wait for post-06-25 HL stabilization.

### `xyz:SOFTBANK` — never scaled — 2026-06-28

- **Observed on strats:** `gf3/japan/am`, `gf3/japan/pm`
- **Inflection date:** deployed then immediately broke (peak 06-28 am,
  07-01 pm); very short baseline
- **What changed:** insufficient data to characterize — deploy was new;
  broke before we had enough baseline to see a shift.
- **Impact:** own PnL of −$2,093 (am) and −$1,454 (pm) in ~7 sessions.
- **Current status:** **ACTIVE but flagged** — REAL_BLEEDER in fleet
  review. Decision pending: kill or investigate why never scaled.
- **Related studies:** `studies/fleet_review_20260715.md`
- **Baseline for redeploy:** would need more sessions of clean history
  to distinguish "regime issue" from "never had edge here." Suspect
  the latter given both am and pm broke immediately.

---

## Cluster observations

### 2026-06-25 tech mega-cap event — CONFIRMED trigger 2026-07-15

Three symbols (NVDA, INTC, MSFT) hit their peak cumulative on the *exact
same session*, then broke on the next session. Two strat variants
(gf1/usday, gf2/usday_cov_0521) — independent configs — but same trigger
date. HL micro-structure diverged from topbook differently per symbol
(NVDA/INTC quieter than topbook; MSFT busier than topbook), but the
common thread is *some* HL-side change per symbol on 2026-06-25.

**Confirmed driver (2026-07-15 investigation)**:

- **Micron (MU) earnings beat** reported after close 2026-06-24: EPS
  $25.11 vs $20.83 est, revenue $41.46B vs $35.85B est. MU +17%
  premarket 06-25, dragging the AI-chip complex higher: NVDA +1.4%,
  AMD +4%, INTC +5%. This drew fresh institutional flow into HL for
  chip-linked names.
- **Apple + Microsoft product-price-hike announcements** same session:
  iPhone (AAPL) and Xbox (MSFT) price hikes announced. MAG7 fell on
  the news; AAPL stock dropped. This produced MSFT-specific and
  AAPL-specific HL participant changes on top of the chip-complex flow.

Mechanism: new institutional flow arrived on HL unevenly per symbol,
producing the symbol-specific HL fingerprints (NVDA/INTC quieter than
topbook — flow direction; MSFT deeper inside + wider spread — new maker
arrival). Our RelWideMM2 strats were tuned to the pre-06-25 HL
participant mix; the new mix invalidated the tuning.

Sources: [TipRanks — AI chip stocks NVDA, AMD, INTC rising 06-25](https://www.tipranks.com/news/why-ai-chip-stocks-nvda-amd-intc-are-rising-today-june-26-2026),
[TheStreet — MAG7 falls after Apple, Microsoft price hikes](https://www.thestreet.com/stock-market-today/stock-market-today-dow-jones-sp-500-nasdaq-updates-june-25-2026).

Related followups:
- Investigate 2026-06-25 concretely
- Build an HL-topbook divergence monitor (`(HL notional/day) /
  (topbook notional/day)` normalized to a rolling baseline; flag when
  |z| > 2)

### 2026-06-25 broader tech mega-cap HL scan (post-cluster review)

Cross-scan on 2026-07-15 of tech mega-caps not yet in the regime log
(`--weekdays-only` bad/base ratios, base 06-01→06-24 vs bad 06-25→07-14):

**Group A: "deeper inside + wider spread" (professional maker arrival,
MSFT/AAPL-like):**

| sym | notional | inside_liq | spread | med_trdsz |
|---|---:|---:|---:|---:|
| AVGO | 0.95× | 2.16× | 1.03× | 1.54× |
| META | **3.89×** | 1.55× | 1.37× | **2.36×** |
| AMZN | 1.43× | 0.98× | 1.24× | 1.80× |
| ORCL | 0.67× | 2.19× | 1.06× | 2.30× |
| NFLX | 1.23× | 1.40× | 0.97× | 0.78× |

**Group B: "quieter + thinner" (participants left, NVDA/INTC-like):**

| sym | notional | inside_liq | spread | med_trdsz |
|---|---:|---:|---:|---:|
| GOOGL | 0.62× | 0.83× | 1.22× | 1.16× |
| TSLA | 0.89× | **0.46×** | 1.07× | 1.14× |
| TSM | 0.78× | 0.62× | 1.06× | 1.14× |

**Not affected**: LLY (all ratios 0.9–1.1×).

Surprising survivors — deep-inside fingerprint but still earning:
- AMZN on `gf1/usday`: own +$1,709
- ORCL on `gf2/usday_cov_0521`: own +$698
- These may have a signal robustness (or luck) worth investigating.

Small-margin cases to monitor:
- META on `gf2/usday_cov_0521`: own +$38 (barely positive, inheritance-cushioned)
- NFLX on `gf2/eq_gf2_cross/v1`: own −$203 (cross variant hurting)

### 2026-06-25 survival drill — AMZN and ORCL survived same HL shift

Investigated 2026-07-15. Both AMZN (`gf1/usday` own +$1,709) and ORCL
(`gf2/usday_cov_0521` own +$698) showed the same "deeper inside +
wider spread + larger trades" HL fingerprint that killed MSFT/AAPL on
2026-06-25 — same strat/config as MSFT for ORCL (`usday_cov_0521`) —
yet kept earning. Markouts on all four stayed benign (~zero bps mo_60s).
The divergence was **fill_rate**:

| sym | fill_bp pre → post | outcome |
|---|---|---|
| MSFT | 20-35 → **70** (2×) | died |
| AAPL | 27-35 → **60-70** (2×) | died |
| AMZN | 10-30 → 15-30 (flat) | survived |
| ORCL | 46-77 → 46-82 (flat) | survived |

MSFT and AAPL got aggressively hit — resting orders lifted at 2× the
baseline rate — while AMZN/ORCL fill rate stayed flat despite the same
HL structural shift.

**Connection to news**: MSFT (Xbox), AAPL (iPhone), NVDA/INTC (chip
rally on Micron beat) were all directly named in 2026-06-25 news.
ORCL and AMZN were not. Hypothesis: the "new maker" arrival was
catalyzed by news and drove *specific-name* taker traffic. Symbols not
in the news cluster got HL spillover (structural shift) but not the
aggressive new taker.

**Actionable ideas**:
1. **News-day size cut** — automated: pull earnings/news calendar;
   reduce size_mult (or widen place_thresh) for that sym on that session.
2. **Fill-rate regime detector** — z-score per-day fill_bp per sym
   against 30-day baseline; alert when it spikes 2×. Would have flagged
   MSFT/AAPL on 06-25 as they broke.
3. **Post-shift monitoring** — after a news event, watch for 5+ days of
   elevated fill_rate; if persists, disable that sym.

The kill mechanism is *frequency* of fills at slightly-adverse marks —
not per-fill markout badness. Extending `hl_divergence_monitor.py` to
include a per-strat `fill_bp` z-score alongside HL micro-structure
z-scores is a natural fit.

### 2026-06-22 → 06-28 broader HL structural shift

The fleet review (`fleet_review_20260715.md`) surfaced 9 ALWAYS_BAD
kills that all had HL micro-structure changes in this window: EWY, MU,
BIRD, NVDA (2×), MSFT (4×), DKNG. Different symbols, different
fingerprints, but a shared week.

Related followups:
- Cross-check other RelWideMM2 syms *not* on the ALWAYS_BAD list —
  did they also see HL micro-structure changes in this window?
  (E.g. QNT, SMSN, INTC).
- Correlate with US-equities cancel-RTT degradation (which per
  `us_equities_cancel_rtt_degradation_202606.md` started 2026-06-22)
  — was there a common HL / network / infrastructure cause?
