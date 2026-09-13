# Playbook: Generating a New Weekend Strat

Instructions to future-Claude when the user asks to add a new symbol (or set
of symbols) to the weekend correlation strategy. The architecture is fixed:
`SigRetEms` per-symbol return EMS → `SigLinear` combines predictors with
regression betas → `corr_alpha` feeds `WideMM` as `pred_sig`.

Read `README.md` in this dir before deploying anything live — protection
defaults and the rationale for which symbols get which knobs are there.

## Where things live

- Generation/sim scripts: `~/scratch/weekend_claude/*.py`
- Per-symbol sim configs: `~/scratch/weekend_claude/sim_alldays_persymbol/{sym}/conf.json`
- Best-params per symbol: `~/scratch/weekend_claude/sweep_persymbol/best_params.json`
- Combined live config: `~/scratch/weekend_claude/pk_weekend_corr_all.json`
- Sim binary: `/home/pktrade/tradefi/retraded_weekend/bin/pktrade`
- Returner (for return CSVs): `/home/pktrade/tradefi/retraded/bin/returner`
- L2 historical data: `~/tardis_datasets/gzpbf/Hyperliquid/`
- Python: `/home/pktrade/.venvs/v1/bin/python`
- Build: `PATH=/home/pktrade/.venvs/v1/bin:$PATH ./compileit.sh`
  (system cmake is too old; the venv has 3.31)

## Workflow

### 1. Confirm market data exists

```bash
ls ~/tardis_datasets/gzpbf/Hyperliquid/ | grep -i <SYM>
```

If missing, use `md_exists.py` (in toolbins) to download. Without L2 history
you can't build the SigLinear or sim — stop here.

### 2. Find correlated predictors

`full_universe_corr.py` regresses each symbol against all others and writes
top-5 predictors by R² to `~/scratch/weekend_claude/results/`. For symbols
already in the main universe, the file will already include them. For new
ones outside it, use `add_extra_symbols.py` which appends them.

Pick predictors by R²:
- R² ≥ 0.2 — strong, the symbol has a good predictor set
- R² 0.1–0.2 — borderline, worth trying but watch sim PnL closely
- R² < 0.1 — weak, expect noisy signal (NATGAS at R²=0.04 was one of the
  symbols that ended up disabled)

### 3. Generate the per-symbol config

Pattern from `gen_configs.py` and the existing per-symbol configs:

- One `pktrader` block: `traded_symbol`, `enabled: true`, `size_mult: 1` for
  sim, `risk` block (incl. `bleed_detector`), `ordex` block of type
  `WideMM` with the standard parameter set, then `signals` array.
- Signals: a `SigRetEms` per predictor and one for the target, then a
  `SigLinear` combining them. Sign convention: `beta_i * ret_ems(predictor_i)
  - 1.0 * ret_ems(target) = residual alpha`. Wire as `pred_sig: corr_alpha`,
  `ref_sig: xyz_<SYM>_mid`.

Easiest: copy an existing sim config from a similar-character symbol (thin
xyz like HYUNDAI for thin books, AAPL/SP500 for thick books) and swap the
symbol name + predictor betas.

### 4. Tune WideMM params

`sweep_persymbol.py` runs three mechanism families (vol_widen additive,
vol_norm, spreadscale_v2) crossed with `place_thresh` and `curv_impulse`
values on the symbol's **most volatile** weekend date. Find that date with
`find_volatile_dates.py`.

Memory: across the 42-symbol initial sweep, `vol_widen` won 31 of 42
symbols. Default to that family if undecided.

Output: best params written into the symbol's
`sweep_persymbol/best_params.json` entry.

### 5. Validate across all weekend dates

`sim_alldays_persymbol.py` runs the symbol's best-params config across the
~38 weekend dates in the dataset. Write its config to
`sim_alldays_persymbol/{sym}/conf.json` (this is also what
`build_combined_config.py` pulls from later).

Look at `all_results.csv` for the symbol. Red flags (from `~/CLAUDE.md`):

- **Sudden drop to very few trades/day** — feed/signal issue, NOT a strategy
  problem. Likely wrong contract month mapping or stale remote signal.
- **Trading only one direction** — large persistent basis, usually wrong
  contract month.
- **Fill rate → 1.0 with tiny notional** — stuck at maxpos, can only exit.

Don't tune the strategy if you see these — fix the data layer first.

### 6. Size scaling

`sweep_sizemult.py` grids `size_mult ∈ {0.25, 0.5, 1, 2, 3, 4, 5, 6}` × all
weekend dates. You're looking for:

- The largest size_mult where Sharpe stays roughly flat.
- Which symbols break at higher sizes (memory: COIN, HOOD, MSFT, MU, TSM
  broke historically) vs scale cleanly (CRWV, SNDK, HYUNDAI, AMD, CL, INTC,
  COPPER, USAR, PALLADIUM scaled to sm=6).
- Whether the symbol stays positive at all — disable if it's negative at
  sm≥2 even after best-params tuning.

### 7. Decide enable / disable

Disable a symbol from live if any of:

- Negative total PnL across the 38-date sim
- Win-day rate < 50% combined with high variance
- Predictor R² too low to give a meaningful signal
- Goes negative as size scales up

Past disable list (point-in-time, may have changed): BTC, HYPE, MU, XYZ100,
MSFT, NATGAS, HOOD. Several of these flipped to net-positive once the
protection stack was added — re-evaluate before assuming a historical
disable still applies.

### 8. Decide protections

Read `README.md` in this dir for what each protection does and when it
fires. Default-on candidates for any new symbol you're enabling live:

- `min_foreign_inside_mult: 2.0`
- `local_size_cap_mult: 3.0`, `local_size_cap_tdc_ms: 60000`
- `risk.bleed_detector` block (it's already in the standard `risk` block —
  don't accidentally drop it when copying)

`exec_anchored_mid_tdc_ms: 180000` is more aggressive (PnL up but order
count up sharply on USAR-class symbols). Apply selectively to symbols where
the rate-limit headroom can absorb 2× orders.

To compare with vs without across the universe:
`~/scratch/weekend_claude/sim_countermeasures/sweep_all_symbols.py` runs
baseline vs protected for every symbol × date. Re-run it before relying on
old numbers — the sided-EMA refactor may have shifted things.

### 9. Combine into live config

`build_combined_config.py` reads each symbol's
`sim_alldays_persymbol/{sym}/conf.json`, applies a final live `size_mult`
(currently 0.125), and writes `pk_weekend_corr_all.json`. The combined file
includes every symbol; `enabled: true/false` controls the live subset.

### 10. Stage the deployment

Don't enable a new symbol at the same size as established ones on the first
weekend it goes live:

- First weekend at `size_mult: 0.125` (or whatever the conservative live
  default is at the time).
- Watch the bleed detector logs (`pktrade.INFO` will show widen/stop
  events). False positives → tune `bleed_score_widen` higher (less
  sensitive). True positives → leave it.
- After a clean weekend, can scale up if `sweep_sizemult` showed headroom.

## Common gotchas

- **Don't push to `pk_weekend_corr_all.json` without a live diff review** —
  the user maintains this file and may have hand-edits not in the
  generation scripts.
- **Match the symbol naming the live config uses** — `xyz:HYUNDAI`, `BTC`
  (no `xyz:` prefix), `HYPE` (no prefix). The combined config is the
  authoritative spelling.
- **Sim doesn't reproduce attack days faithfully** — replayed L2 contains
  our own historical orders, no adversarial response. Don't conclude
  "protections cost too much" purely from sim — they're insurance.
- **Build needs the venv cmake** — `system cmake is 3.22, required is 3.24+`.
  Always prepend `PATH=/home/pktrade/.venvs/v1/bin:$PATH` when running
  `compileit.sh`.

## Quick checklist for a single new symbol

1. [ ] L2 data present in `~/tardis_datasets/gzpbf/Hyperliquid/`
2. [ ] Predictor regression run; R² ≥ 0.1
3. [ ] Per-symbol config written to `sim_alldays_persymbol/{sym}/conf.json`
4. [ ] `sweep_persymbol` run on most volatile date; best params applied
5. [ ] `sim_alldays_persymbol` baseline shows positive total PnL, no red flags
6. [ ] `sweep_sizemult` shows safe scaling envelope
7. [ ] Protections decided (default: sparse + cap + bleed; execmid optional)
8. [ ] Added to combined config via `build_combined_config.py`
9. [ ] First-weekend size kept at conservative `size_mult`
10. [ ] Live results reviewed before scaling up
