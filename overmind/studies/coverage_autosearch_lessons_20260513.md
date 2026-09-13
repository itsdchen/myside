# Coverage Autosearch Lessons — May 2026

Date: 2026-05-13

Lessons from the May 2026 round of `coverage_autosearch` on HIP3 thin/uncovered symbols (HYUNDAI, JPY, SKHX, SMSN, BIRD, DKNG, HIMS) using `RelWideMM2` ordex with `vol_norm` widening. Companion to `equities_autosearch_lessons_202605.md` which covers `alpha_relwide_autosearch`.

Sweep workdirs:
- `~/scratch/coverage_autosearch/20260423/` — initial round (CRCL, MSTR, PLATINUM, PLTR, RIVN, TSLA, BABA, COPPER, etc.)
- `~/scratch/coverage_autosearch/20260511/` — round 2 (Korean A/B, JPY, BIRD/DKNG/HIMS post data ingestion)

## 1. place_thresh auto-range clamps to single value when base > PARAM_BOUNDS max

The auto-ranger does ±20% around the base config value, then clamps to `PARAM_BOUNDS["place_thresh"] = (0.0001, 0.003)`. If base value is above 30 bps, the clamp collapses the range to a single value.

CRCL US Day was the canary in 2026-04-23: base `place_thresh=0.004` → auto-range `[0.0032, 0.0048]` → clamped → `[0.004]` only. Sweep ran 500 variants but with 1 thresh value, 375/500 didn't trade, top sim_score was 0.12. Re-run with manual `[0.0012-0.003]` produced 476/500 traded, top score 6.55.

Same trap on TSLA US Day (base too high) and BIRD (base 22 bps).

**Mitigation:** added `coverage_autosearch.py --scan-thresh PATH` mode (2026-05-13). It runs a place_thresh-only scan over `[0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02]` (1-200 bps log scale) per spec entry, holding all other dims at base defaults. Use it before `--run-spec` to size proper ranges. Output lands in `<spec_dir>/thresh_scan/`.

Workflow is now: `--generate-spec` → `--scan-thresh SPEC` → manually pick ranges → `--run-spec`.

## 2. enabled=False trap

`coverage_autosearch.run_spec` filters the base config to the target symbol's pktrader and sets `size_mult=1` — but does **not** force `enabled=True`. If the base config's pktrader has `enabled: False` (e.g. deprecated/disabled in production), the sim runs cleanly but the trader is inert: 0 orders, 0 trades.

Hit by **MSTR** and **PLATINUM** in 2026-04-23 round. Both came from combined configs where they were disabled. Results.txt had 500 rows of all-zero PnL.

**Fix:** either patch base config to set `enabled: True`, or add to fixed overrides in spec. Currently the patched base approach (`pk_*_patched.json`) is the convention.

The `scan_thresh` function added in 2026-05-13 forces `enabled=True` automatically — `run_spec` should arguably do the same.

## 3. beta_uncertainty_coef and rel-fields required by newer RelWideMM2

Older single-symbol configs (e.g. `gf1/pltr/rel_btc_longbreak/pk_pltr_rel_btc_allday.json`) predate the rel-uncertainty refactor and lack:
- `beta_uncertainty_coef`
- `relative_sig`, `relative_beta`, `snapshot_interval_s`, `hardcoded_min_tick`, `last_perm_snapshot_*.old`

Loading such a config into the current `bin/pktrade` triggers a RapidJSON assertion in `RelWideMM2::RelWideMM2` reading the missing field, producing `Aborted (core dumped)` at sim startup. The `Aborted` line is **stderr-buffered** so it severely undercounts in the log — actual abort rate was ~100%, visible as 78% in stdout.

**Detection:** check `results.txt` for `num_trds == 0` in *all* variants. That's the unambiguous "sims didn't run" signal, regardless of how many "Aborted" lines you see.

**Fix:** patch the base config with sensible defaults (see `pk_*_patched_v2.json` in `20260511/`):
```python
o.setdefault('relative_sig', f'local_mid_{short}')
o.setdefault('relative_beta', 0.0)
o.setdefault('beta_uncertainty_coef', 0.0)
o.setdefault('snapshot_interval_s', 60)
o.setdefault('hardcoded_min_tick', 0.01)
o.setdefault('last_perm_snapshot_rel_px.old', 0)
o.setdefault('last_perm_snapshot_remote_px.old', 0)
```
With `relative_beta=0`, the rel signal is a no-op on price prediction.

## 4. Secmaster gates new HIP3 symbols

New HIP3 listings (BIRD, DKNG, HIMS in this round) need entries added to `src/pktrade/util/secmaster_lib/HyperliquidSec.h` before they sim. Without an entry, `pktrade::TradeCarrier::postSecmaster()` throws `Symbol (Hyperliquid, xyz:BIRD) not found in secmaster.`

This is silent in coverage_report (which counts traders, not whether they sim) and silent in `coverage_autosearch --generate-spec`. You only find out at run time.

**Process:** add entries by hand to `HyperliquidSec.h` (copy a similar symbol's block, fill in symbol name, sym_idx, tick_size, lot_size from Hyperliquid's `meta` endpoint), then **rebuild both `pktrade` and `returner`**. `compileit.sh pktrade` doesn't rebuild `returner` automatically.

## 5. TopBookEquity remote feed is the silent-fail trap

The `RelWideMM2` strategy needs both local (Hyperliquid) and remote (TopBookEquity) data. If the remote feed has no historical data for a symbol, the sim runs cleanly, the trader is enabled, but `our_cross 0 their_cross 0 their_add 0` — strategy never has a remote tick to anchor a fair value, so it places no orders.

`coverage_autosearch.run_spec` only calls `md_exists --market <local_mkt>` (i.e. `Hyperliquid` for HIP3). It does **not** fetch the remote feed. Symbols whose local feed is fresh and remote feed is empty look like they're working but produce zero trades.

Hit by BIRD/DKNG/HIMS in 2026-05-11. The fix:
```bash
md_exists.py --sym BIRD --market TopBookEquity --start YYYYMMDD --end YYYYMMDD
```
Verify presence with `find ~/tardis_datasets/gzpbf/TopBookEquity -name "<SYM>_quotes_*.gzpbf"`.

**Open follow-up:** patch `coverage_autosearch.run_spec` (and `scan_thresh`) to fetch both local AND remote feeds when the trader has a remote_sig.

## 6. md_exists S3 manifest can lie about availability

`md_exists` lists "Getting X from aws (N total). Proceed?" based on a local manifest of expected files. The actual S3 bucket (`l1-pktrade-capture`) may not have those keys — downloads then 404 silently and print only `DL failed for X`.

For BIRD on `TopBookEquity` in 2026-05-12, md_exists offered to fetch 2 files but both 404'd. Verified with `boto3.list_objects_v2(Prefix='gzpbf/TopBookEquity/BIRD_')` returning empty. The data simply didn't exist in S3 yet — required out-of-band ingestion.

**Mitigation:** when md_exists "DL failed" loops on multiple dates, do a quick boto3 listing to distinguish "S3 has it but download is broken" vs "S3 doesn't have the data".

## 7. Random sampling is seeded — re-runs don't explore new variants

`stratbuilder.SimVariations.sim_grid` does `random.seed(42)` before `random.sample(range(N), max_variants)`. Re-running the same spec with the same dim product picks the same indices.

So `--run-spec` with `resume=True` after wiping a results dir re-runs identical variants, not new ones. To explore additional regions of the variant space, you need to either:
- Change a dim (forces a new total `running_quotient`, different sample indices), or
- Change the seed (currently hardcoded — would require code change).

## 8. Hyperliquid native perps are NOT `xyz:` prefixed

In `coverage_autosearch.py --symbols` and the rest of the system, the convention is `xyz:<SYM>` for HIP3 symbols and bare `<SYM>` for native HL perps:
- `xyz:SMSN` — HIP3 Samsung
- `BTC`, `NQ` — native HL perps
- `xyz:BTC` does NOT exist — returner returns empty, secmaster lookup fails

The `hip3_corr.py` smoke test ran with `xyz:BTC` and `xyz:NQ` — got zero data. Bug if you forget the convention.

## 9. Returner is mid-only, 1-min sampled

`bin/returner` writes `time_str, time_s_utc, mid_px, return` per minute. Bid/ask are sampled but only mid is output. For spread analysis, need a different tool.

The 1-min returns at HIP3 thin liquidity are mostly microstructure noise; meaningful beta only emerges at 5-15 min resample. SMSN ↔ EWY 1-min R² was 0.0017; 15-min R² was 0.14.

## 10. Korean stocks: BTC vs EWY as relative — A/B result

Tested 3 Korean HIP3 stocks (HYUNDAI, SKHX, SMSN) with both BTC-rel (`SigMid BTC Hyperliquid`) and EWY-rel (`SigQuoteMid EWY TopBookEquity`) variants. Common dim grid, only the rel signal differed.

Top-5 average sim_score:

| symbol | BTC-rel | EWY-rel | winner |
|---|---|---|---|
| HYUNDAI | 2875 | 1385 | BTC by ~2x |
| SKHX | 728 | 747 | tied (EWY: 1.37 sharpe vs BTC 0.49) |
| SMSN | 1606 | 411 | BTC by ~4x |

Counter to the prior hypothesis (EWY as a Korean ETF should track Korean names well during their session). Likely explanations:
- HL synthetic EWY may not perfectly track Korean equity moves
- BTC's higher volatility provides more correlation signal in absolute terms even with weaker fundamental link
- Korean stocks on HIP3 may have direct BTC-flow correlation (HL trader flow)

## 11. Sweep cost / system contention

Full 10-entry × 500-variant × 14-day sweep with 11 dims = ~70k sims. Wall time on a contended box (other `retraded_4` alphavol jobs running): ~24 hours. On a clean box: probably ~6-8 hours.

If the system load average exceeds the core count by >2x, consider pausing parallel work or accepting the slowdown.

## 12. coverage_autosearch.py changes baked in this round

- `DEFAULT_TUNE_PARAMS` extended: `cancel_buffer`, `pred_momentum_coef`, `premium_tdc_s`, `place_thresh_mode`
- `PARAM_BOUNDS` and `PARAM_TYPES` extended for the above
- `DEFAULT_FIXED_OVERRIDES` updated: removed `premium_tdc_s: 10` (now swept), added `restrict_near_book: True`
- New `--scan-thresh PATH` mode for pre-flight place_thresh probe
- `generate_spec` now iterates `DEFAULT_FIXED_OVERRIDES` instead of hardcoding `max_back_levels` only

## Per-symbol findings (round 2, 2026-05-11)

Cross-symbol pattern first, then per-symbol notes from the top-5 of each entry.

### Cross-symbol pattern in winners

- **`vol_norm_coef=0.1` dominates** — the lowest value in the sweep wins for ~all symbols. The vol_norm mechanism is providing essentially no value at the parameter levels we tested. Either the mechanism needs revisiting or higher coefs need to be tested (we capped at 5.0).
- **`ladder_one_sided=True` was forced** for this round and is consistent with everything we know about winners.
- **`place_thresh_mode`** splits: thin/sparse names (BIRD, DKNG, HIMS, JPY, SKHX) prefer **mode 0** (the older spread-blind mode); higher-flow Korean names (HYUNDAI, SMSN) prefer **mode 1** (spread-aware).
- **`premium_ema_coef`**: 0.9 or 1.0, never lower. The dim range `[0.9, 1.0]` was correctly chosen.
- **Top sharpes line up with `100% positive days`** consistently (HYUNDAI BTC, DKNG, JPY all hit 1.0 pos with sharpes 1.4-1.5+).

### BIRD US Day (~$5 stock, just-listed, thin)

- Best `place_thresh=0.003` (30 bps) — wider than all other equities tested. The earlier scan had hinted at 50 bps as best score, so 30 bps in the full grid still in the right neighborhood.
- **All winners use `per_order_widen_frac=0.35`** — well below the 0.5-0.6 range used by every other symbol. BIRD is so thin that adding lots of widening kills order placement.
- `ema=0.9-1.0`, `cancel_buffer=0.2-0.4`, mixed `pred_momentum_coef`. No clear curv pattern.
- 0.87 sharpe, 80% positive days. Solid but only 14-day sample window — fragile.

### DKNG US Day (~$24 stock, recently listed)

- Best `place_thresh=0.0002` (2 bps) — the **tightest** in the round, capitalizes on tight spread.
- **`pred_momentum_coef=0` in all top-5**, except one with 0.5. Pred-momentum signal doesn't help DKNG.
- `ptdc=10` (fast premium reaction) universal.
- `cancel_buffer` varies (0.1-0.5) — not a strong driver.
- Sharpe **1.36-1.44** in top-5, 100% positive days in top-3. Cleanest sharpe story of the new symbols.

### HIMS US Day (~$26 stock, recently listed)

- Best `place_thresh=0.0002-0.0005` (2-5 bps), tight like DKNG.
- **High trade counts (106-239 vs 50-100 elsewhere)** — but LOW sharpe (0.11-0.29). Lots of small trades, low edge per trade.
- `ema=0.9` universal. `ptdc=120` (slow premium) universal — opposite of DKNG.
- `pred_momentum_coef=0` universal.
- 83% positive days consistently. PnL ~$30-90/day — modest but additive.
- HIMS profile is "high-frequency low-edge" — different from DKNG's "low-frequency high-quality". Worth an operational call on whether the trade volume is desirable.

### HYUNDAI All Day BTC-rel — winner of HYUNDAI A/B

- Best `place_thresh=0.0012` (12 bps).
- **Sharpe 1.10-1.59 in top-5, 100% positive days in all top-5**. Strongest profile of the entire sweep.
- `ema=0.9-1.0`, `cancel_buffer=0.1-0.5` (no clear winner), `pred_momentum_coef` varies (0, 0.5, 1, 3).
- `place_thresh_mode=1` in 3/5 top variants (spread-aware paying off).
- `vol_norm_coef=0.1` universal.
- Top variant: $1250 avg PnL/day, 164 trades. By far the highest-PnL entry.

### HYUNDAI All Day EWY-rel — loses to BTC

- Same `place_thresh=0.0012-0.0016` as BTC.
- `ptdc=120` universal in top-5 (BTC top winners had mixed ptdc).
- Sharpe 0.74-0.95, 80-90% positive — *good*, just not as good as BTC's 1.0+ pos.
- Top sim_score 1465 vs BTC's 3365. About 40% of BTC's score, presumably because EWY tracks Korean equities less tightly than the BTC<-flow correlation that HIP3 sees.

### JPY All Day (FX)

- Best `place_thresh=0.000352` (3.5 bps) — the tightest reasonable for FX.
- **`per_order_widen_frac=0.79-0.88`** — the **highest** widen_frac of any symbol. JPY's tight spread + wide rung spacing works.
- `ptdc=120` and `pred_momentum_coef=0` in 4/5 top-5.
- Sharpe 1.51 / 100% positive days for top-3. Small absolute PnL ($25/day) but reliable.

### SKHX All Day BTC-rel

- Best `place_thresh=0.002` (20 bps).
- **`curv_impulse_coef=0` in all top-5** — curv signal actively hurts SKHX. Distinct from HYUNDAI/SMSN where curv=1.25 wins.
- `premium_tdc_s=10` (fast reaction) universal — also distinct from HYUNDAI/SMSN.
- `place_thresh_mode=0` dominant.
- Lower sharpe (0.30-0.74) than HYUNDAI/SMSN. SKHX is harder to trade cleanly even though it's the same Korean cohort.

### SKHX All Day EWY-rel — wins SKHX A/B on quality

- Same `place_thresh=0.002` as BTC variant.
- **Sharpe 1.00-1.72** vs BTC's 0.30-0.74 — much cleaner profile.
- `ema=1.0`, `ptdc=10`, `vol_norm_coef=0.1` universal in top-5.
- Top sim_score 898 ~= BTC's 925, so scores tie. EWY wins on consistency.
- **Operational pick: SKHX EWY** even though scores match — less noise.

### SMSN All Day BTC-rel — winner of SMSN A/B

- Best `place_thresh=0.002` (20 bps).
- Top variant pattern matches HYUNDAI BTC closely (`ema=0.9, curv=1.25, ptdc=120, mode=1`).
- Sharpe 0.71-1.13, 80-90% positive. Strong but a step below HYUNDAI's perfection.
- Top sim_score 1741 — second-highest in the sweep behind HYUNDAI BTC.

### SMSN All Day EWY-rel — loses to BTC by 4x

- Same `place_thresh=0.002`.
- Sharpe 0.37-0.66, 60-80% positive — clearly weaker than BTC variant.
- Top sim_score 476 vs BTC 1741. The largest BTC-vs-EWY gap of any Korean stock.

### Symbol families

- **HYUNDAI, SMSN — BTC-rel works great** (1.4 sharpe, 100% pos for HYUNDAI; 0.9-1.1 sharpe for SMSN). Same param family: ema=0.9, curv=1.25, ptdc=120, mode=1. Treat as a group.
- **SKHX — distinct from HYUNDAI/SMSN**: prefers `curv_impulse_coef=0` and fast `ptdc=10`. Possibly because SKHX has different microstructure (memory chip pricing dynamics?). Don't lump SKHX in with HYUNDAI/SMSN tuning blindly.
- **DKNG, HIMS — both tight-thresh equities**, but very different temperaments. DKNG = sparse high-quality (sharpe 1.4), HIMS = busy low-edge (200+ trades, sharpe 0.2). Same auto-pick template won't work.
- **BIRD — outlier on `per_order_widen_frac`** (0.35 vs everyone else's 0.5-0.6). Future BIRD-like (very thin, low-priced) names should likely use widen_frac < 0.5 by default.
- **JPY — outlier on `per_order_widen_frac` the other way** (0.79-0.88, the highest). FX in general probably wants wider rung spacing.

### Param-tier learnings worth baking into defaults

- **`vol_norm_coef=0.1` always wins** in this round → consider lowering the default sweep range for `vol_norm` mode (or test much higher values like 10+ to see if there's a hidden second peak).
- **`place_thresh_mode=0`** is fine for sparser symbols, **mode 1** wins for higher-flow names — sweep both, no clear universal default yet.
- **`per_order_widen_frac` should be a per-symbol parameter** (or at least be auto-ranged more aggressively) — values vary 0.35 to 0.88 across the sweep, so a uniform `[0.5, 0.6]` range is too narrow.

## Open follow-ups

- **Patch `run_spec` to fetch remote feed too** when trader has `remote_sig` on a non-local market. Eliminates the silent "0 trades" failure for new symbols.
- **Patch `run_spec` to force `enabled=True`** like `scan_thresh` already does. Avoids the MSTR/PLATINUM trap.
- **Dynamic random seed** option for `sim_grid` to enable broader exploration on re-runs.
- **Auto-detect single-value place_thresh dims** in spec generation and emit a warning (or trigger scan_thresh as a side-effect).
- **Add `cancel_buffer_frac`** to `coverage_autosearch` dims if it ever applies to RelWideMM2 (currently AlphaRelWideMM-only).
