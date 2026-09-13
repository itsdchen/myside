# feedquality — reading guide

A reference for everything written by the `feedquality` binary
(`src/pktrade/toolbins/feedquality.cc`) and the
`feedquality_analyze.py` script
(`overmind/strat_main/tools/feedquality_analyze.py`).

The binary measures **how good each external market data feed is at
predicting the price on Hyperliquid (HL)**. HL is "ground truth" not
because it's the most accurate price, but because predicting it is the
job — the strategy trades against HL. The analyzer aggregates one day's
output into derived metrics across many days.

---

## 1. The big picture

Two complementary axes of feed quality:

| Axis | Question | Captured by |
|---|---|---|
| **Precision** | When the ext feed shouts (deviates from HL by a lot), is it right? | `_spikes.csv` |
| **Recall** | When HL moves a lot, did the ext feed shout in time? | `_recall.csv` |

A useful feed needs both — high precision means few false signals, high
recall means few missed signals. Either alone is misleading: a feed that
never fires has trivially perfect precision; a feed that fires constantly
has trivially perfect recall.

The 2×2 underneath:

|                          | **HL moved a lot**   | **HL didn't move much** |
|--------------------------|----------------------|-------------------------|
| **Ext diverged from HL** | A: HL_FOLLOWED       | B: SPIKE_REVERTED / BASIS_SHIFT |
| **Ext didn't diverge**   | C: EXT_MISSED        | D: quiet (not measured) |

- Precision = A / (A + B) — measured in `_spikes.csv`
- Recall    = A / (A + C) — measured in `_recall.csv`
- F1 = 2·P·R / (P + R)

---

## 2. Files written per run

Per invocation (one date), the binary writes **four CSVs** under
`<out-prefix>_<date>_*`:

```
<prefix>_<date>_grid.csv      1 Hz synchronized state of all feeds
<prefix>_<date>_spikes.csv    one row per ext-spike event (precision side)
<prefix>_<date>_recall.csv    one row per (HL-spike × ext) pair (recall side)
<prefix>_<date>_summary.csv   per-feed totals at end of run
```

Files are independent — you can analyze any subset.

---

## 3. `_grid.csv` — synchronized 1Hz state

Used for: regression analysis, lead/lag cross-correlation, uptime, anything
where you want all feeds aligned to a common time grid.

| Column | Meaning |
|---|---|
| `time_ms` | UNIX ms timestamp of the grid sample |
| `hl_mid` | Latest HL mid at sample time |
| `hl_age_ms` | Time since last HL update (ms). High value = HL is stale at this sample. |
| `<EXT>_mid` | Latest mid for that ext feed |
| `<EXT>_age_ms` | Time since last update for that ext (-1 if never updated) |
| `<EXT>_basis` | EMA of `(ext_mid - hl_mid)` in price units. The "expected" gap. |
| `<EXT>_sigma_bps` | Stddev of residual (`ext - hl - basis`) in bps of HL mid |

**Interpretation tips:**
- For analysis, filter rows where `hl_age_ms` and `<EXT>_age_ms` are both
  small (e.g., `< 10000`) so you're comparing fresh prices.
- The `<EXT>_basis` column is the slow-moving "fair gap" estimate. For CME
  silver futures vs HL spot it's typically positive (futures premium); for
  PolygonFX spot it should be near zero.
- `<EXT>_sigma_bps` tells you the typical noise level around the basis.
  Spike thresholds use `5 × sigma`, so spike events have `|residual| ≳ 5×`
  this column.

---

## 4. `_spikes.csv` — ext-spike events (precision side)

Used for: classifying whether each ext-feed spike was a real signal or noise.

A "spike" opens a watch when, on a single ext tick:
```
|ext_mid - hl_mid - basis_ema|  >  max(k_sigma · sigma, bps_floor · hl_mid)
```
Defaults: `k_sigma=5`, `bps_floor=3 bps`. Only one watch per feed at a time
(during a watch, no new spikes fire on that feed).

W seconds later (default `W=2s`), the watch is classified.

| Column | Meaning |
|---|---|
| `start_ms` | Watch open time |
| `end_ms` | Watch close time (= start_ms + W·1000) |
| `source` | Which ext feed (e.g., `TopBookCme_SI`) |
| `classification` | `HL_FOLLOWED` / `SPIKE_REVERTED` / `BASIS_SHIFT` (see below) |
| `hl_at_start` | HL mid at watch open |
| `ext_at_start` | Ext mid at watch open |
| `basis_at_start` | Basis EMA at watch open (price units) |
| `sigma_at_start` | Residual sigma at watch open (price units) |
| `r0_bps` | Initial residual `(ext_at_start - hl_at_start - basis_at_start)` in bps of HL — the spike magnitude, signed |
| `r1_bps` | Residual at watch close, in bps of HL — small means ext snapped back |
| `hl_delta_bps` | HL price change during the watch, in bps |
| `ext_delta_bps` | Ext price change during the watch, in bps |
| `sigma_bps` | `sigma_at_start` in bps of HL (for context — ratio `r0_bps / sigma_bps` is the "sigma multiple" of the spike) |
| `hl_at_end` / `ext_at_end` | Prices at watch close |
| `duration_ms` | Should equal `W × 1000` |

### Classification rules

```python
hl_followed = sign(hl_delta) == sign(r0)  AND  |hl_delta| > 0.3 · |r0|
reverted    = |r1| < 0.5 · |r0|

if hl_followed:           HL_FOLLOWED      # ext led a real move
elif reverted:            SPIKE_REVERTED   # ext was wrong, snapped back
else:                     BASIS_SHIFT      # ext stayed dislocated
```

### What each classification means

- **`HL_FOLLOWED`** — the spike was a real signal. Ext jumped, then HL moved
  in the same direction by ≥30% of the spike magnitude within W seconds.
  This is the "true positive" — exactly the signal you'd want a strategy to
  trade on.

- **`SPIKE_REVERTED`** — the spike was a bad tick. Ext jumped, HL didn't
  follow, ext snapped back to its consensus level (residual fell to <50% of
  the spike's magnitude). A strategy listening to this feed would have fired
  a spurious trade. **This is the metric you minimize.**

- **`BASIS_SHIFT`** — ambiguous. Ext jumped, HL didn't follow, ext stayed
  dislocated. Three possible causes:
  1. Real basis shift (futures roll, structural change in the ext-vs-HL
     relationship)
  2. Slow reversion that took longer than W=2s (try `--reversion-w-secs 10`)
  3. EMA still adapting from a previous shift

  Small counts here can be ignored. Large counts mean either real basis
  changes (lower `--basis-tau-secs`) or the W window is too short.

---

## 5. `_recall.csv` — HL-spike events (recall side)

Used for: measuring whether ext feeds predicted the HL moves that
mattered.

A row is written when, on an HL tick, HL has moved by more than the
threshold over the last `--hl-lookback-secs` (default 2s):
```
hl_change_bps  =  (hl_now - hl_anchor) / hl_anchor · 10000
                  where hl_anchor = hl mid 2s ago
trigger if |hl_change_bps| > max(k_sigma_hl · sigma, bps_floor)
```
Defaults: `k_sigma_hl=4`, `bps_floor=3 bps`. Debounced by
`--hl-debounce-secs` (default 2s) to prevent multiple events for one
sustained move.

When triggered, **one row is written per ext feed** classifying whether
that ext predicted the move.

| Column | Meaning |
|---|---|
| `start_ms` | Time the HL spike was detected (now_ms of the triggering HL tick) |
| `hl_lookback_ms` | The window over which `hl_change_bps` was measured |
| `hl_change_bps` | HL return over the lookback window, signed |
| `hl_change_sigma_bps` | Rolling sigma of HL changes over windows of this size — context for "how unusual" the move was |
| `hl_at_start` | HL mid at start of lookback window (the anchor) |
| `hl_at_end` | HL mid now (the spike-trigger price) |
| `source` | Which ext feed this row is about |
| `max_resid_bps_in_window` | Largest `\|residual\|` for this ext during the lookback window, in bps of HL |
| `resid_threshold_bps` | The ext's spike threshold at the trigger time, in bps |
| `classification` | `EXT_LED` if `max_resid_bps > resid_threshold`, else `EXT_MISSED` |

### What each classification means

- **`EXT_LED`** — during the 2 seconds before HL moved, ext's price already
  diverged from HL's old price by enough to qualify as a spike. The ext fed
  a signal in advance. Good — this is recall.

- **`EXT_MISSED`** — HL moved but ext stayed within sigma of HL's old price
  the whole window. Ext gave no warning. Bad.

The recall threshold (`resid_threshold_bps`) is the same threshold the
spike detector uses, so EXT_LED in `_recall.csv` is consistent with what
would have triggered a spike event in `_spikes.csv`.

---

## 6. `_summary.csv` — per-feed totals at end of run

One row per source. The HL row has only the HL-side fields filled; ext
rows have all fields.

| Column | Meaning |
|---|---|
| `source` | Feed label (e.g., `Hyperliquid_xyz_SILVER`, `TopBookCme_SI`) |
| `is_hl` | 1 if this is the HL row, 0 otherwise |
| `tick_count` | Total updates observed for this feed |
| `session_ms` | Length of the session in ms |
| `ticks_per_sec` | `tick_count / (session_ms / 1000)` |
| `ext_spike_events` | Total spike watches that classified for this ext |
| `hl_followed` | Count of HL_FOLLOWED classifications |
| `spike_reverted` | Count of SPIKE_REVERTED classifications (the "bad tick" count) |
| `basis_shift` | Count of BASIS_SHIFT classifications |
| `precision_pct` | `100 × hl_followed / ext_spike_events` |
| `hl_spike_events_total` | (HL row only) Total HL spikes detected this session |
| `led_hl` | Count of EXT_LED for this ext (HL spikes this ext predicted) |
| `missed_hl` | Count of EXT_MISSED for this ext |
| `recall_pct` | `100 × led_hl / hl_spike_events_total` |
| `f1_pct` | `2 · P · R / (P + R)` |

---

## 7. Analyzer output (`feedquality_analyze.py`)

Run after the binary has produced one or more days of output:
```
feedquality_analyze.py --prefix /tmp/feedquality_silver
```
Loads `<prefix>_<date>_*.csv` for every matching date and aggregates.

### `=== Configuration ===`
Echoes which prefix, dates, and feeds are being analyzed.

### `=== Per-feed totals ===`
Direct sum across days of the per-feed counters in `_summary.csv`. The
precision/recall/F1 are recomputed on the summed counters (so they're
weighted correctly).

### `=== Uptime ===`
For each ext feed, the percentage of grid samples where the feed was
"fresh" (age < 60s) and the median age. Tells you whether a feed has
gaps you should worry about (CME's nightly maintenance, weekend FX
holidays, etc.).

### `=== Lead/lag cross-correlation ===`
For each ext feed, the correlation of `ext_return(t)` with
`hl_return(t + lag)` for `lag` in `±max-lag-steps` grid steps.

- **Positive lag with positive corr** = ext leads HL by that many steps.
  E.g., `peak at +1000 ms` with `corr=+0.34` means CME's return at second
  `t` correlates 0.34 with HL's return at second `t+1`. CME is **leading
  by 1 second**.
- **Lag 0 with positive corr** = ext is synchronous with HL.
- **Negative lag** = HL leads ext (rare and usually noise).

This is the cleanest signal in the report. **A feed that leads HL is a
feed your strategy can trade on.**

### `=== Predictive R² of HL returns at lag 0 ===`
- **Single-feed regressions**: how much of the variance in HL's
  contemporaneous return is explained by each ext feed alone.
- **Joint regression**: same but with all feeds together. If one feed's
  beta drops to ~0 in the joint, it adds nothing on top of the others.
- **Joint at lag +1 step**: same regression but predicting HL's return
  one grid step ahead. **If lag-+1 R² > lag-0 R², the ext feeds are
  leading HL.** This is consistent with the cross-correlation peak.

### `=== Ext-spike magnitude (|r0_bps|) percentiles ===`
Distribution of how big spikes are. A feed whose spikes are typically
small probably has more "borderline" detections; one whose spikes are
typically large is finding bigger genuine dislocations.

### `=== HL-spike magnitude percentiles ===`
Distribution of how big the HL moves we tried to predict were. Usually
much smaller than ext spikes (because HL spike threshold is at
`bps_floor=3`; most are tiny).

### `=== Recall by HL-spike size ===`
Recall percentage broken down by how big the HL move was. **This is
where the recall numbers become honest:** small HL moves are essentially
unpredictable spike-wise (because they don't generate enough ext
divergence to qualify as a spike). Large HL moves are predicted much more
often. Read this rather than the headline `recall_pct`.

---

## 8. Worked example: silver, one day

Real output from `feedquality --hl-symbol "xyz:SILVER" --external
TopBookCme:SI --external TopBookEquity:SILVER --date 20260423`:

```
                       precision  recall  F1
TopBookCme_SI            72%       9%    16%
TopBookEquity_SILVER     87%       8%    14%

Lead/lag:
  TopBookCme_SI:        peak at +1000 ms (ext leads HL, corr=+0.34)
  TopBookEquity_SILVER: peak at     0 ms (synchronous,  corr=+0.50)

R²:
  TopBookCme_SI alone, lag 0:        R²=0.0004
  TopBookEquity_SILVER alone, lag 0: R²=0.2549
  joint, lag 0:                      R²=0.2576
  joint, lag +1 step (1s ahead):     R²=0.3015

Recall by HL-spike size (CME):
  HL move <5 bps:    led 3% of the time
  HL move 5-10 bps:  led 14%
  HL move 10-20 bps: led 47%
```

**Interpretation:**

1. **CME leads HL by ~1 second.** That's the headline result — CME's
   return at time `t` correlates 0.34 with HL's at `t+1`. PolygonFX
   doesn't lead, it just moves with HL (peak at lag 0).

2. **PolygonFX has higher precision (87% vs 72%).** When PolygonFX
   shouts, it's right more often. Consistent with PolygonFX being closer
   to a "spot truth" — fewer transient liquidity events.

3. **Joint R² jumps from 0.26 (lag 0) to 0.30 (lag +1).** The combined
   model predicts HL's *next-second* return better than its current-second
   return. That's only possible because at least one feed is leading —
   here, CME.

4. **Recall is low overall but rises sharply with move size.** Tiny HL
   moves (<5 bps) are basically noise both feeds can't predict. Big
   moves (10-20 bps) are predicted ~half the time by CME. The headline
   "9% recall" is misleading without this breakdown.

5. **Bottom line** for this one day: use **PolygonFX as the precise
   tracker** (high contemporaneous correlation, fewer false signals),
   plus **CME as a leading indicator** (1-second lead on the bigger
   moves). A combined feature should work better than either alone.

(Caveats: one day is not enough. Run a few weeks before drawing
conclusions. Also re-run with `--grid-interval-ms 250` to measure
sub-second leads accurately.)

---

## 9. Tunables

CLI flag → parameter it controls → when to change it.

| Flag | Default | Change when… |
|---|---|---|
| `--grid-interval-ms` | 1000 | You want sub-second lead/lag resolution. Try 250 or 100. Cost is bigger CSV files. |
| `--k-sigma` | 5 | Too few/many ext spikes. Lower = more spikes (more false positives), higher = fewer (more selective). |
| `--bps-floor` | 3 | Too many spikes during low-volatility periods (raise) or missing real ones (lower). Roughly: should be a few × your typical spread. |
| `--reversion-w-secs` | 2 | Many BASIS_SHIFTs that you suspect are slow reversions. Try 5 or 10. |
| `--staleness-secs` | 60 | A feed has known long gaps you want to handle (e.g., FX weekends — raise this). Or you want to be stricter about uptime (lower). |
| `--basis-tau-secs` | 300 | Basis is shifting faster than 5 minutes (lower). Or basis EMA is too jumpy (raise). |
| `--vol-tau-secs` | 60 | Vol regime changes are slow (raise) or fast (lower). |
| `--hl-lookback-secs` | 2 | The HL move window for recall. Larger = catches slower HL moves (and gives ext more time to have led). Smaller = faster, more selective. |
| `--hl-debounce-secs` | 2 | Two HL spikes too close together. Raise to dedupe more. |
| `--k-sigma-hl` | 4 | Too few/many HL spike events. |

---

## 10. Adding new symbols / feeds

The binary is symbol- and market-agnostic. Any `Market:Symbol` registered
in `mktdata.py` works:

```bash
# Gold:
feedquality --hl-symbol "xyz:GOLD" \
  --external TopBookCme:GC --external TopBookEquity:GOLD \
  --date 20260423 --out-prefix /tmp/fq_gold

# ETH (HL perp vs Binance USDT futures):
feedquality --hl-symbol ETH \
  --external BinanceFutures:ETHUSDT \
  --date 20260423 --out-prefix /tmp/fq_eth
```

Required: data files exist under
`~/tardis_datasets/gzpbf/<Market>/<Symbol>_<channel>_<date>.gzpbf` for
each (Market, Symbol) on the session's UTC days. If missing, the binary
prints a warning at startup pointing at `md_exists.py` for download.
