# Alpha RelWide Autosearch Draft

## Why we need this
- alpha_rel_wide_mm is the most sensitive ordex to missing pk configs: it expects working TradeArmory bundles (signals + tempos + lair regressions) before SimVariations or pnl climbs can start.
- Today the flow stalls because ~half of the XYZ symbols do not have a ready `pk_*.json` and even fewer have fresh regressions; every new symbol requires manual scavenging in `~/scratch/metalclimb_*/*` plus full recalibration.
- Goal: a low-touch autosearch that starts with “give me XYZ” and produces candidate configs without lots of hand edits.

## Stage 0 — locate or mint a base pk.json
Stage 0 can’t rely solely on `metalclimb_*` outputs because many symbols never ran there (and new ordex rollouts won’t have live configs yet). We need layered fallbacks:
1. **Template + model-bank override (primary path)**
   - Start from a generic pk template checked into the repo (future: trade confs). Swap in the symbol-specific `model_bundle.json` from the bank (pred/ref IDs already normalized), so the result is runnable even if no prior pk exists. This is the default for brand-new symbols.
2. **Optional discovery**
   - If desired, scan `~/scratch/metalclimb_*` and/or live trading configs to find existing pk files referencing `alpha_rel_wide_mm`. Treat these as bonus seeds when they exist, but never block on them.
3. **Gap filling**
   - Regardless of the source, inject default ordex parameters when missing (e.g., `want_cross`, `cross_v2_*`, `place_thresh`, `enter_thresh`, `max_back_levels`, `per_order_widen_frac`). Emit a manifest (`seed_manifest.json`) describing which knobs were inferred vs. sourced so later steps know where to focus the sweep.

## Stage 1 — build/refresh the regression bank
- **Why**: alpha_rel_wide_mm refuses to run without a regressed model; missing models are the block today.
- **Approach**:
  1. Define a rolling list of target symbols (e.g., all HIP3 + coverage gaps).
  2. For each symbol, launch a “historical regression kit” job that:
     - Chooses a recent 20–30 trading day window (pull from coverage_report) and reruns the TradeArmory stack using inherited lair inputs (see `overmind/modelbuild_confs/xyz_relwide_armory*.json`).
     - Produces: lair coefficients, PCA assets, `signalscanner` sampling files, and pk.json pointing to these outputs.
  3. Store outputs in a predictable bank, e.g., `~/scratch/relwide_modelbank/YYYYMMDD/SYMBOL/{pk.json, signals/, tempos/, regressions/}` plus a small metadata file (date range, reg alpha, score).
  4. Tag whether the regression used same-day or inherited signals so we can judge freshness.
- **Usage in autosearch**:
  - When Stage 0 can’t find a pk.json for XYZ, grab the newest entry from the model bank (even if generated headlessly) and use that as the base.
  - If the bank lacks XYZ entirely, the autosearch CLI can offer an opt-in `--bootstrap-regression` flag that queues the historical regression job first, then reruns Stage 0 after it finishes.

## Stage 2 — SimVariations sweep (for later)
- Once Stage 0 + Stage 1 guarantee a runnable pk.json, feed it into SimVariations using the same spec flow as `coverage_autosearch` (dims/range_dims + overrides).
- Specs can reference the base config’s current values, so when we do discover an existing pk we can express variations as “±X% around current” instead of absolute numbers; brand-new configs can use hard-coded presets.
- Add a `--max-variants` and `--sample-size` control so even if the cartesian product yields, say, 2,000 combinations, we can deterministically subsample 500 (e.g., evenly spaced downsampling or seeded random sample) before handing them to `sim_grid()`.
- Provide presets (“core thresholds”, “curv phase”, “cross phase”) similar to the other autosearch script but tweakable via CLI flags or spec edits.
- CLI sketch:
  - `alpha_relwide_autosearch.py --generate-spec --symbol XYZ --preset core --sim-days 14 --max-variants 800 --sample-size 400`
  - `alpha_relwide_autosearch.py --run-spec path/to/spec.py --sample-size 400 --downsample-mode even`
  - `--preset` toggles which param groups appear in the spec (core thresholds, ladder/curv, cross/TL2). Multiple presets can be combined.
  - `--base-config PATH` lets you override the automatic Stage 0 seed if you have a custom pk.
- Downsampling modes:
  - `even` (default): order variants by multi-index, then take every Nth element so coverage across each dimension remains uniform.
  - `random --seed N`: shuffle variants deterministically with RNG seed, take the first `sample_size`.
  - `score-guided`: when re-running, start with the top `K` from the previous summary plus a random sample of the remaining variants.
- Spec generation merges live values (if seed pk existed) with defaults: `range_dims` entries automatically set `center=current_value`, `neighborhood_pct` per param, and `min/max` clamps to ordex-safe ranges. When no live value exists, fall back to preset centers.

## Open questions / next steps
1. **Catalog format** — decide whether to throw everything into a SQLite db vs. flat json. Start with json for simplicity.
2. **Regression scheduling** — integrate with existing TradeArmory automation? Could reuse the coverage gap list to schedule nightly “missing bank” jobs.
3. **Quality filters** — before accepting a banked regression, enforce minimum fit stats (markout cap, Sharpe). Otherwise, Stage 0 should skip it and fall back to scaffolding.
4. **Glue code** — factor config/dim builders shared with `coverage_autosearch.py` to avoid duplication once Stage 2 begins.

## Saved Model Bank Plan
- **Storage layout**: dedicate `~/scratch/relwide_modelbank/` (and a repo copy under `overmind/model_inheritance/`) with per-build stamps (`YYYYMMDD`) and per-symbol folders. Each folder stores a single combined JSON (e.g., `model_bundle.json`) that wraps the regressed pred signal plus its companion ref/mid series. Within the bundle the signal IDs are normalized (e.g., `pred_SYM`, `ref_SYM`) so any consumer can drop them into a pk config without caring about the original metalclimb filenames. No pk.json, lair blobs, or tempos live here.
- **Metadata schema**: `{ "symbol": "XYZ", "built_at": "20260312", "date_range": ["20260201", "20260221"], "remote_sym": "NQ", "reg_style": "enetcv", "quality": "provisional|validated", "score": 1.3, "source": "metalclimb_20260312" }`. Include checksum hashes for pk + lair outputs so consumers can detect drift.
- **Freshness policy**: default TTL 30 calendar days; nightly cron scans metadata and flags stale entries. CLI (`alpha_relwide_autosearch.py --list-bank`) shows freshness state so users know if bootstrap regressions are needed.
- **Bootstrap workflow**: when Stage 0 can’t find a valid pk.json, it first checks the bank for symbol-specific model bundles. If missing/stale, it can launch `coverage_autosearch`-style regression jobs via a helper script (`relwide_bank_rebuild.py`) that reuses TradeArmory configs and regenerates the pred/ref JSON bundle (plus metadata) into the bank path. The CLI prints the queued job id so users can monitor progress.
- **Quality tiers**: mark entries as `provisional` when derived from generic inherit-only fits; once a symbol completes a proper historical regression with good fit stats, upgrade to `validated`. Stage 0 prefers validated entries but can fall back to provisional ones (emitting a warning).
- **Promotion path**: after SimVariations finds a winning ordex override set, write back a `promotion.json` (symbol, variant id, bank entry used). A follow-up helper can bake these overrides into the stored pk.json and tag the bank entry as “deployed”.
- **Garbage collection**: keep the latest `N=3` bank entries per symbol. Older ones are pruned automatically unless explicitly pinned (e.g., `metadata.json` contains `pin: true`).

### Helper workflow (import vs. auto-regress)
- Ship a single CLI (`relwide_model_bank.py` or similar) with two subcommands:
  1. `import` — assumes a finished TradeArmory run already produced `coef_lair/pred_sig.json` + `ref_sig.json`. Arguments: `--symbol`, `--venue`, `--source-dir`, optional metadata overrides. The tool loads those jsons, normalizes IDs (to `pred_SYM`/`ref_SYM`), wraps them into `model_bundle.json`, writes metadata, and registers the entry in both `overmind/model_inheritance/...` and the scratch bank. Use when we already have a working regression.
  2. `autobuild` — takes `--symbol`, `--venue`, `--start-date`, `--end-date` (remote-symbol mappings live elsewhere, e.g., a `symbolizer.py` helper). It spawns the minimal regression workflow automatically:
     - Picks/creates a TradeArmory config (reuse templates in `overmind/modelbuild_confs/*relwide_armory*.json`), fills in the traded symbol plus the requested window, and runs the regression only for that symbol (no remote proxying during bootstrap).
     - Executes the regression job (metalclimb-style) targeting a temp scratch dir.
     - On success, immediately invokes the `import` path against the freshly produced `coef_lair` dir.
- Both flows emit the same artifacts (bundle + metadata) so downstream consumers don’t care where the model came from. The only difference is whether we pointed at an existing regression or asked the helper to generate one with minimal manual effort.
- Maintain a simple catalog (e.g., `overmind/model_inheritance/catalog.json`) that lists, per symbol/venue, the latest bundle path, build date, quality tier, and hash. The helper updates this index after each import/autobuild, and Stage 0/autosearch consults it to locate the freshest valid model.

- `pybin/alpha_relwide_autosearch.py check-bundles --symbols SYM1,SYM2` now wraps the bank scan/autobuild dance: it inspects `~/scratch/relwide_modelbank` plus the repo mirror, reports age/quality, surfaces the last `~/scratch/relwide_autobuild/` job, and reruns `--bootstrap-models` automatically when `--bootstrap-models` is passed. This keeps missing bundles from surprising long sweeps.
- `pybin/alpha_relwide_autosearch.py run-spec ...` writes every sweep into `results/warehouse.sqlite3` (runs table + per-symbol stats + serialized top-k rows) and emits a summarized `best_variants.json` (per symbol: winning variant id, metrics, scratch/pk paths). Downstream tooling can diff runs via sqlite instead of parsing `results.txt` ad hoc.
- `pybin/alpha_relwide_autosearch.py onboard-symbol --symbol xyz:NEW --spec-path /path/to/spec.py` adds a new symbol to any existing spec: it (optionally) bootstraps regressions, seeds a base config (respecting presets + ordex type), appends the symbol entry, and rewrites `run.sh` so you can immediately rerun the multi-symbol sweep. Use `--force` to refresh an existing entry.
- RelCross sweeps plug into the same CLI: pass `--auto-ordex-type RelCross` (or `--symbol-ordex xyz:FOO=RelCross`) and the generator will pull configs out of `overmind/for_live/saved_strats` (override via `--saved-strats-root`). Presets such as `relcross_core,relcross_passive` mirror the old `gen_relcross.py` grid, and `pybin/md_exists.py` now runs automatically before the spec is written so missing historical data doesn’t nuke the sweep. These runs reuse the same warehouse + `best_variants.json` flow but also write `aggregate_results.csv` for the "best variant per symbol" summary that the standalone script used to dump.
- Symstats is consulted whenever a base config is auto-scaffolded, so `place_thresh`/`base_cross_thresh` grids center around recent spreads and auto-generated configs double their variant budgets + neighborhoods, making the first sweep much more exhaustive without manual tuning.
