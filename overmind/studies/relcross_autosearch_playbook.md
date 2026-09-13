# RelCross Autosearch — Iterative Playbook

Date: 2026-07-01

Living playbook for tuning **RelCross** across the HIP3 universe via a phased,
session-aware autosearch with in-sweep OOS reservation. Modeled on the RelWide
Equities Autosearch playbook (`relwide_equities_autosearch_playbook.md`) with
adaptations for the cross-liquidity strategy and the lessons from the
20260629 retrain flake-out (`relcross_unseeded_retrain_20260616.md` §
Implications for the next sweep).

Companion tool: `overmind/strat_main/tools/trademan/relcross_autosearch.py`
(to be built; this doc is the design spec).

Working dir convention: `~/scratch/relcross_retrains/run_<big_batch_date>/<YYYYMMDD>_<slug>/` for sweep workdirs; `~/scratch/relcross_retrains/deploy/run_<date>/` for deploy state.

---

## 1. Goal

Produce a per-symbol, session-appropriate RelCross config with an OOS-verified
edge, using a repeatable process that:

1. Never picks a variant on data it was trained on.
2. Ranks variants by robust (median-based, filter-gated) metrics — not
   outlier-driven mean.
3. Maps each symbol to its native session window before sim, so Asian /
   overnight / commodity syms don't silently fail.
4. Stages final picks under human review before touching the live-facing run dir.

The tool wraps `SimVariations` + `coverage_autosearch`; it does not fork the
grid-search engine. It adds RelCross-specific defaults, session mapping,
OOS reservation, and a narrowing loop.

---

## 2. Phased methodology

### Phase 0 — `anchor-scan` (multi-dim, coarse)
Coarse multi-dim sweep over the RelCross primary anchors. Purpose is to
locate the symbol's neighborhood in the biggest levers before Phase 1's
refined sweep. Other dims pinned to donor / prior-round values.

| Anchor | Values | Why |
|---|---|---|
| `base_cross_thresh` | `[0.00005, 0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01]` (8) | primary trigger threshold |
| `premium_tdc_s` | `[15, 60, 240, 1200]` (4) | bimodal per prior playbook — need to know which peak the sym lives at |
| `vol_norm_coef` | `[1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 16.0]` (7) | most powerful vol-relative scaler (`widen = coef × σ_per_√s × pred_px`). Range extended to 8.0 after 2026-07-04 SILVER investigation showed that on the 6/17-6/25 basis-blowup event, no variant with `vnc ≤ 3.0` survived. Further extended to 16.0 on 2026-07-06 after gf0 sweep showed 4/6 24h commodities concentrating robust picks at `vnc=8.0` — the ceiling was still clipping. Other vol mechanisms (`vol_widen_coef`, `vol_ratio_coef`, `snr_thresh_coef`) strictly worse or harmful per Apr 2026 postmortem. |
| `vol_norm_tdc` | `[30, 60, 120, 240]` (4) | interacts with vol_norm_coef; 5 is catastrophic, 60 is cross-symbol sweet spot, 240 extended for slow-rebuild vol windows on stressed commodities |

Cartesian: 8×4×4×3 = **384 variants per sym.** Full sim window, but
Phase 0 selection uses train portion only (Phase 0 sizes Phase 1's
grids; doesn't pick a live variant).

**Skipping Phase 0** collapses the auto-ranger onto the donor's anchor
values, which is wrong for most cross-symbol clones — the RelCross
Apr 2026 postmortem showed anchor values vary massively across syms
(threshold 0.0003 → 0.001, vol_norm_coef 1.0 → 3.0).

### Phase 1 — broad sweep
Multi-dim sweep. **Phase 0-anchored dims are tightened around Phase 0's
winner** (2-3 values each). **Free dims** are searched fresh.

**Phase 0-anchored (tighten around Phase 0 winner):**

| Param | Values |
|---|---|
| `base_cross_thresh` | 3 values at Phase 0 winner ×0.5 / ×1 / ×2 |
| `premium_tdc_s` | winner + adjacent from `[15, 60, 240, 1200]` (2-3 values) |
| `vol_norm_coef` | winner + adjacent from `[1.0, 1.5, 2.0, 3.0]` (2-3 values) |
| `vol_norm_tdc` | winner + adjacent from `[30, 60, 120]` (2-3 values) |

**Free dims (searched fresh):**

| Param | Values | Source |
|---|---|---|
| `exit_adjust` | `[0.3, 0.5, 0.7]` | exit_adjust grid rule (±0.1) |
| `premium_ema_coef` | `[0.6, 0.8, 1.0]` | capped at 1.0 per memory rule |
| `remote_mom_coef` | `[0, 0.15, 0.3]` | 10/57 syms picked 0 last round |
| `feed_lag_widen_coef` | `[0, 1.5]` | binary on/off per prior playbook |
| `precog_miss_max_adjust` | `[0.85, 1.0]` | split 30 off / 27 active |

Cartesian ≈ 3 × 3 × 3 × 3 × 3 × 3 × 3 × 2 × 2 = ~2000 variants;
`random_sample=True`, `max_variants=500` — ~25% grid coverage per sym,
vs 1.7% in the 20260629 retrain.

**New candidate dims** (not in default; add on a per-sym round or as a
Round B study):
- `hold_decay_floor` + `hold_decay_tdc_s` (currently 0.1 / 0, off)
- `curv_impulse_coef` `[0, 1.25]` — bimodal on Korean syms, unstudied elsewhere
- `pred_momentum_coef` + `pred_momentum_tdc` — RelWide finds it valuable
- `ms_between_cross` — throttle axis for congestion resilience

### Phase 2 — narrowed sweep
Reads Phase 1 `results.txt`, applies quality bar, computes per-dim
concentration among **healthy** variants scored on the **OOS window
only**. Lock a dim to a single value when ≥70% of healthy variants
concentrate there; otherwise keep the top 2-3 values.

**Preserve-extremes rule** (added 2026-07-04): for regime-sensitive
dims — `vol_norm_coef`, `vol_norm_tdc`, `feed_lag_widen_coef`,
`base_cross_thresh` — the narrow step also carries the highest value
along even if the top-N by count didn't include it. This prevents
the OOS-tail window from blinding Phase 1 to the "regime-defense"
zone of the parameter space. SILVER's 6/17-6/25 basis blowup would
have benefited from `vol_norm_coef=8.0`; the naive top-3 narrow
dropped `vnc=3.0` because the calm OOS-tail didn't like it.

**Prefer-higher-defensive tiebreak** (added 2026-07-06): for
regime-defensive dims — `vol_norm_coef` and `feed_lag_widen_coef` —
when the top-2 counts are within 15% of each other, prefer the higher
value. Motivation: hitting a higher-vol regime out-of-sample is
asymmetric — losing a bit of edge in calm times is much cheaper than
blowing up in a storm. When the sweep says two values are essentially
tied, err toward the defensive one.

Rerun with the narrowed spec; repeat 1-2 rounds until dims stabilize or
Phase 2 fails to find enough healthy variants.

### Phase 3+ — judgement / promotion
Inspect Phase 2 winner for regime fragility (< 20% healthy rate → sparse),
IS/OOS trade-rate ratio (> 3× → overfit smoking gun), and per-sym signals
(e.g., is `feed_lag_widen_coef=1.5` because the sym is congestion-sensitive,
or noise?). Promote to `promoted/pk_<sym>_v<vid>.json` under human review.
Then the promote script copies winners into `run_<date>/sources/`
for final diff review before `build_per_machine.py --run-slug run_<date>`
regenerates `by_machine/`, and `ln -sfn run_<date> current` cuts over live
proper and rebuilds per-machine configs.

---

## 3. OOS discipline

**The 20260629 retrain flake-out was caused by**: (a) picking variants on
the *same window* they trained on, then (b) validating on a backward
window (misinterpreting adjacency as OOS), then (c) discovering forward
OOS on a single day of data was too noisy to discriminate.

**New rule:** OOS days are reserved *inside* the sim window from the start.

```
sim_days      = 25         # target total window; may be shorter for
                           # recently-listed syms
oos_days_tail = 5          # fixed; the last 5 days of whatever window
                           # the sym actually has
```

- `sim_days = 25` is the default target. For recently-listed syms the
  tool falls back to the longest window available (from sym listing
  date through sweep-end date), as long as that window contains at
  least `oos_days_tail + min_train_days` trading days (default
  `min_train_days = 10`).
- If the available window is shorter than `oos_days_tail +
  min_train_days`, the tool **errors out for that sym** rather than
  quietly running with a stub train — this is the guardrail against
  another AVGO/QNT/NOW-style "just a few days of data, pick anyway"
  situation.
- `oos_days_tail = 5` is fixed regardless of window length. Selection
  never uses fewer OOS days than 5.
- Sim runs on all available days in the window.
- Selection ranks variants on **OOS-tail-only** stats: `oos_avg_pnl`,
  `oos_median_pnl`, `oos_pct_positive`, `oos_num_trds`, `oos_sharpe`.
- Concentration narrowing uses the OOS-tail-only healthy variants.
- IS stats are logged but not used to rank picks. This eliminates
  "pick on data it trained on."

**Post-sweep revalidation (optional):** the OOS-tail is genuine held-out
data, so it's the primary check. A second forward revalidation is only
worth doing when there's a meaningful gap between the sweep window and
the deploy time (e.g., sweep ended > 5 trading days before deploy), or
when the sym's OOS-tail happened to land on unusually low-vol days.
Otherwise skip — the OOS-tail is enough.

---

## 4. Selection metrics — kill outlier-driven ranking

Ranking by `avg_pnl` alone is fragile: one $1800 outlier day can drive
a $50/d strategy to look like a $180/d strategy (see BIRD in the
20260629 retrain, real median $46 vs `avg_pnl` $184).

**Selection rule** (applied to OOS-tail + IS stats):

1. **Default rank: `oos_median_pnl` descending.** Tiebreak by
   `oos_sharpe`, then by `oos_pct_positive`. Median resists the
   single-outlier-day-drives-the-pick failure mode.
2. **Alternative rank: `--rank robust` (regime-aware).** Added
   2026-07-06 after silver 6/17-6/25 basis-blowup showed
   `oos_median_pnl` alone can favor variants that got lucky in the
   OOS window despite catastrophic IS behavior. Robust rank applies
   these hard filters and then ranks:
   - `is_avg_pnl > 0` (must be profitable overall, not just OOS-tail)
   - `oos_avg_pnl > 0`
   - `is_worst_5d ≥ -$50` (worst rolling 5-day IS window)
   - Score: `is_avg_pnl + oos_avg_pnl + max(0, worst_5d + 50) × 0.1`
3. **Every variant is annotated with 7 quality-bar flags** (compact
   format, expanded in the tool's legend header):
   - `PP` — `oos_pct_positive ≥ 0.60`
   - `NT` — `oos_num_trds ≥ 20` (~ borderline 10-20; - < 10)
   - `FR` — `oos_fillrate ≥ 0.005`
   - `AV` — `oos_avg_pnl > 0`
   - `RR` — IS/OOS trade-rate ratio ≤ 3× (> 3× is an overfit tell
     per RelWide §6c9)
   - `IA` — `is_avg_pnl > 0` (profitable overall)
   - `DD` — `is_worst_5d ≥ -$30` (regime-fragility check; hits
     `DD-` when any 5-day IS window loses more than $30 —
     silver-motivated)
4. **Tool presents top-N (default 20)** — ranked list with all signals
   visible — and the human picks. **No auto-selection.** Picks are
   qualitative decisions informed by the metrics, not the metrics
   themselves. When multiple picks share similar rank scores, prefer
   the one with more `+` flags overall (esp. `DD+` and `IA+`).
5. For concentration narrowing (Phase 2), the "healthy variants" pool
   defaults to top 100 by `oos_median_pnl` — but the user can override
   with a looser or tighter set as the sym's regime demands.

---

## 5. spec.py schema

Same shape as RelWide but with RelCross defaults and additional
`oos_days_tail` field. Sessions keyed by display name; supported values
map 1:1 to `build_per_machine.py`'s machine windows.

```python
# RelCross Autosearch Spec — <round name / date>
# Donor: <xyz:SYM used to seed missing configs>

sim_days = 25
oos_days_tail = 5
max_variants = 500
random_sample = True
neighborhood_pct = 0.3      # for range_dims narrowing

default_dims = [
    (["pktraders", "ordex", "base_cross_thresh"], [0.0001, 0.0002, 0.0005, 0.001, 0.002]),
    (["pktraders", "ordex", "premium_tdc_s"], [15, 30, 240, 300]),
    (["pktraders", "ordex", "vol_norm_tdc"], [15, 60, 240]),
    (["pktraders", "ordex", "vol_norm_coef"], [1.0, 1.5, 2.0]),
    (["pktraders", "ordex", "exit_adjust"], [0.3, 0.5, 0.7]),
    (["pktraders", "ordex", "premium_ema_coef"], [0.6, 0.8, 1.0]),
    (["pktraders", "ordex", "remote_mom_coef"], [0, 0.15, 0.3]),
    (["pktraders", "ordex", "feed_lag_widen_coef"], [0, 1.5]),
    (["pktraders", "ordex", "precog_miss_max_adjust"], [0.85, 1.0]),
]

default_fixed = [
    (["pktraders", "ordex", "flip_through"], False),
    (["simulation", "Hyperliquid", "lag_aware_latency"], True),
    (["simulation", "Hyperliquid", "ioc_lag_base_ms"], 900),
    (["simulation", "Hyperliquid", "ioc_lag_coef"], 2.0),
]

default_range_dims = []     # optional: dims narrowed as ±neighborhood_pct

symbols = {
    "xyz:LLY|US Day": {
        "base_config": "/home/pktrade/scratch/relcross_retrains/deploy/current/sources/pk_lly.json",
        "session": "US Day",
    },
    "xyz:BRENTOIL|24h": {
        "base_config": ".../pk_brentoil.json",
        "session": "24h",
    },
    # KRX/JPX syms enter twice — once per half-session; each is swept
    # independently against its own trading window.
    "xyz:KR200|KRX AM": {
        "base_config": ".../pk_kr200.json",
        "session": "KRX AM",
        "clone_from": "xyz:SKHX",     # optional — Korean-session donor
    },
    "xyz:KR200|KRX PM": {
        "base_config": ".../pk_kr200.json",
        "session": "KRX PM",
        "clone_from": "xyz:SKHX",
    },
    ...
}
```

Session values are registered in `relcross_autosearch.SESSION_WINDOWS`:

```python
SESSION_WINDOWS = {
    "usday":  ("US Day",  ("09:35:00 America/New_York", "17:00:00 America/New_York")),
    "24h":    ("24h",     ("18:00:00 America/New_York", "17:00:00 America/New_York")),
    # KRX has a 12:00-13:00 KST lunch break; sweep AM and PM as separate
    # sessions so the sim window matches actual trading.
    "krxam":  ("KRX AM",  ("20:00:00 America/New_York", "23:00:00 America/New_York")),
    "krxpm":  ("KRX PM",  ("00:00:00 America/New_York", "02:30:00 America/New_York")),
    # JPX has a 11:30-12:30 JST lunch break; same treatment.
    "jpxam":  ("JPX AM",  ("20:00:00 America/New_York", "22:30:00 America/New_York")),
    "jpxpm":  ("JPX PM",  ("23:30:00 America/New_York", "02:00:00 America/New_York")),
}
```

These mirror `build_per_machine.py`'s machine settings. The
launcher must patch each sym's `pk_template["settings"]["start_t"]` and
`end_t` from its session before dispatching to SimVariations. **If a
sym is listed without a session key, the tool errors out** rather than
silently using a default — this is the guardrail against the 06-29
Asian-sym silent-fail.

---

## 6. Tool commands (planned)

```
relcross_autosearch.py init-spec --workdir DIR --donor xyz:SYM --syms xyz:A,xyz:B --session usday
relcross_autosearch.py anchor-scan --spec DIR/spec.py
relcross_autosearch.py run-spec --spec DIR/spec_round_a.py
relcross_autosearch.py narrow --spec DIR/spec_round_a.py --out DIR/spec_round_b.py
relcross_autosearch.py revalidate --spec DIR/spec_round_b.py --oos-start YYYYMMDD --oos-end YYYYMMDD
relcross_autosearch.py promote --spec DIR/spec_round_c.py --to DIR/promoted/
```

Each command:
- Validates every sym has a `session` key (fails loud if not).
- Registers session in `coverage_autosearch.SESSION_TIMES` before dispatch.
- Verifies `md_exists` for local + remote feed for every date in the
  sim window (RelWide lesson: silent 0-trade fails otherwise).
- Uses a unique workdir slug per invocation (RelWide lesson:
  `feedback_simvariations_unique_workdir_slug.md`).
- Writes results under `results/<sym>/<spec_name>/results.txt` matching
  RelWide's layout.

---

## 7. Deploy safety pattern

Deployable configs are organized as **dated runs at the top of the workspace**,
with a `current` symlink for the live process to follow. Each run is a
self-contained snapshot of the per-sym source pks + the per-machine deploy
configs generated from them, plus a single `STATS.md` writeup.

```
~/scratch/relcross_retrains/deploy/
  build_per_machine.py                    # tool
  current -> run_<date>                   # live reads from current/by_machine/
  run_<date>/                             # one snapshot per retrain cycle
    sources/pk_<sym>.json                 # per-sym source configs
    sources/pk_<sym>_pm.json              # for AM/PM split syms
    by_machine/pk_cx_<machine>_1.json     # deployable per-machine configs
    STATS.md                              # picks, metrics, sweep provenance
  run_<older_date>/                       # prior snapshots kept for rollback
```

**Workflow per retrain cycle:**

1. Sweeps produce per-variant `pk_full.json` under `~/scratch/relcross_retrains/run_<batch_date>/<sweep>/results/…/scratch/<vid>/`.
2. Human picks winners (see §4). Copy into `run_<new_date>/sources/pk_<sym>.json`
   — starting from a copy of the previous run's `sources/` so untouched syms carry over.
3. Force `size_mult=1` on every newly-promoted pk (see size rule below).
4. Run `python build_per_machine.py --run-slug run_<new_date>` — reads sources,
   writes `by_machine/`.
5. Regenerate `STATS.md` (per-machine summary + per-sym pick metrics with
   sweep dir provenance).
6. `ln -sfn run_<new_date> current` — cuts over live.
7. Old runs stay as `run_<older_date>/` for rollback (just repoint the symlink).

**AM/PM split syms** — see §9 (Asian-session infra). Each session runs as its
own pktrader in its own machine (`gf3_kor` for KRX AM, `gf3_kor_pm` for KRX PM,
etc.) since pktrade dedupes multiple pktraders on the same `traded_symbol` in
a single machine. `build_per_machine.py` routes `pk_<sym>_pm.json` files to
the `<machine>_pm` variant.

**Configs live in scratch, not in the repo.** All pk files — sources and
by_machine — stay under `~/scratch/relcross_retrains/deploy/`. Never move
them into `overmind/for_live/` or anywhere else checked into the repo. The
live deploy reads directly from `current/by_machine/`. Repo-tracked files
are limited to the launcher, the tool, the playbook, and
`build_per_machine.py` — never per-sym or per-machine pk files.

**Size rule: always deploy new picks at `size_mult=1`.** Even if the sym's
current live config uses `size_mult > 1`, a freshly-tuned config goes live
at 1× per-order sizing regardless. Ramp up only after several days of live
behavior confirming the sim results. The promote script must force
`size_mult=1` on every pk before writing to `sources/`; the `STATS.md`
should record the prior size_mult so the ramp-back decision has context.

---

## 8. Warts to avoid (from RelWide + our own experience)

1. **Session microstructure inflation** (RelWide §6d1-d4): equities
   start-time of 09:30 inflates sharpe 40-60% via open-auction noise.
   Use 09:35 or 09:40 for US Day.

2. **Silent MD failure**: RelWide observed sims running cleanly on 0
   trades when the remote feed was missing. Always pre-check
   `md_exists` for the sim window.

3. **Sparse-name fragility**: symbols with < 20% healthy-variant rate
   are regime-sensitive. Flag; consider small live sizing.

4. **Trade-rate explosion**: IS/OOS trade-rate ratio > 3-5× is a
   near-certain overfit tell (RelWide §6c9). Discard variants that fail
   this check.

5. **Don't over-prune negative variants** during narrowing (RelWide
   feedback): a later round may turn them positive. Filter for
   concentration analysis, not for dropping from the sweep.

6. **Change ≥1 dim between rounds** (RelWide §6c10): `sim_grid` uses
   `random.seed(42)`. Re-running the same spec re-picks the same
   variants. Narrowing must genuinely change the search space.

7. **Don't rank by `avg_pnl` alone** (our 20260629 lesson + RelWide
   §6b1): use median-based OOS ranking with a pct_positive gate.

8. **Session-appropriate sweep window** (our 20260629 Asian-sym
   silent-fail): launcher must set `start_t`/`end_t` per sym's session
   before dispatch.

9. **Post-hoc revalidation is a fallback, not mandatory.** OOS-tail is
   the primary check. Revalidate on a forward window only when there's
   a meaningful gap between sweep-end and deploy time (> 5 trading days)
   or when the OOS-tail landed on unusually low-vol days.

10. **Full-quality checklist before promotion** (RelWide §8.1): every
    promoted variant needs a sanity read of (a) pct_positive, (b)
    daily-pnl histogram (not just mean), (c) trade-rate stability, (d)
    IS/OOS trade-rate ratio, (e) per-day rollup showing no single-day
    outlier drives the ranking.

---

## 9. How this differs from the prior RelCross retrain flow

The 20260629 retrain used an ad-hoc bash launcher
(`launch_retrain_0629.sh`) calling `SimVariations.py` directly per
symbol, then a Python compile script picked winners from `results.txt`
by sim_score. This playbook replaces that.

| Concern | Prior flow (20260629) | New autosearch flow |
|---|---|---|
| **Search structure** | Single-round 9-dim grid, 500 random-sampled per sym | Phased: Phase 0 anchor scan → Phase 1 broad → Phase 2 narrow → Phase 3 judgement |
| **Session mapping** | Single US-day pk_template applied to all syms | Per-sym `session` field in spec.py; tool patches start_t/end_t before dispatch; errors if session missing |
| **KRX/JPX handling** | Silently ran with US-day window → empty results.txt | Half-session entries (KRX AM/PM, JPX AM/PM) with correct NY-time windows |
| **OOS discipline** | Ranked on IS avg_pnl; OOS was a separate post-hoc single-variant re-sim | OOS days reserved inside the sim window; ranking uses OOS-tail-only stats |
| **Rank metric** | `avg_pnl` (or sim_score which is close) | `oos_median_pnl` with quality-bar signals; tool presents top-N, human picks |
| **Grid design** | Wide grid over all 9 dims; `premium_tdc_s` included the 60/120 sparse zone; `feed_lag_widen_coef` continuous | Phase-0-anchored dims narrowed around anchor winner; sparse-zone values dropped; `feed_lag_widen_coef` binary |
| **Concentration analysis** | Not done — compile script picked one variant per sym | Phase 2 narrows dims by ≥70% concentration among healthy variants |
| **Deploy path** | Direct edit of per-sym pk file + rebuild per-machine | Dated `run_<date>/{sources,by_machine,STATS.md}` snapshots with `current` symlink for rollback |
| **Robustness against outlier days** | Weak — one $1893 day drove BIRD from $46 median to $184 avg | Median-based ranking; quality-bar filters (fillrate, ntrd, pct_pos) shown per variant |

**What stays the same:**
- Underlying grid engine (`SimVariations.py` + `coverage_autosearch`)
- The 9 core ordex dims live in the swept axis list
- Per-sym unique workdir slug (avoids the acct-file contamination bug)
- HYPE heartbeat + lag-aware sim defaults from `sim_lvl_inst.h` (900, 2.0)

## 10. Iteration logbook

*(Filled in as work progresses. Each entry: date, workdir, syms
covered, phase reached, picks promoted / declined and why.)*

_(none yet — this playbook was drafted 2026-07-01 after reverting the
20260629 retrain applies)_
