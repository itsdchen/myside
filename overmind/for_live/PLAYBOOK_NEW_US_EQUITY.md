# Playbook: Adding a New US Equity (Hyperliquid hip3)

End-to-end checklist for wiring a new US equity (single-stock or ETF)
into trading. Based on the GME/LITE/XLE/BX/MRVL/RKLB/DRAM/EWZ/ZM/EBAY
batch added 2026-05-04 → 2026-05-06.

Replace `<SYM>` with the uppercase ticker (e.g. `EBAY`) and `<sym>` with
the lowercase form (e.g. `ebay`) throughout.

---

## 1. Generate per-symbol spread stats

Produce `for_live/per_eq_stats/<sym>_stats.txt` (12-day historical
table of avg spread / vol per 30-min bin, ET). Used to derive
`place_thresh` per session.

## 2. Compute `place_thresh` per session from spreads

For each session window, take the **volume-weighted mean spread (bps)**
across the bins inside the window:

| Session | Bins (ET) |
|---|---|
| usday   | 09:30 – 15:30 (13 bins) |
| 2usday  | 09:30 – 15:30 (same as usday) |
| postusa | 16:00 – 17:30 (4 bins) |
| rel_nq  | 18:00 – 19:30 + 04:00 – 09:00 (15 bins) |

Map vwap → `place_thresh` ≈ **1× spread** (in bps / 10 000):
- 7 bps  → `0.0007`
- 60 bps → `0.0060`

**Caps / floors:**
- `rel_nq_allday`: cap at `0.008` (80 bps). Wide overnight spreads
  would otherwise push thresholds so far the bot rarely posts inside.
- usday/2usday: same value (sharp 2usday ladder uses same threshold as
  usday in this batch).
- postusa: no cap; the 16:00 close-auction bin can dominate vwap and
  push values to ~150 bps for some names.

## 3. Create per-symbol conffiles

Directory: `for_live/saved_strats/<sym>/`

Four files, all using `RelWideMM2` ordex:
- `pk_<sym>_usday.json`
- `pk_<sym>_2usday.json`
- `pk_<sym>_postusa.json`
- `pk_<sym>_rel_nq_allday.json`

**Easiest path:** copy a recent batch dir (e.g. `saved_strats/gme/`),
search-replace the symbol literals (`xyz:GME` → `xyz:<SYM>`,
`gme` → `<sym>`, `GME` → `<SYM>` for the `remote_sig` symbol field),
then patch the per-symbol values below.

### Notional targets

| Variant | `tgt_order_notional` | `tgt_maxpos_notional` | `risk.max_notional` |
|---|---|---|---|
| usday   | 2000  | 20000 | 20000 |
| 2usday  | 2000  | 20000 | 20000 |
| postusa | 2000  | 20000 | 20000 |
| rel_nq  | 1000  | 10000 | 10000000 (effectively unlimited) |

`max_pos` (shares) ≈ `tgt_maxpos_notional / price`,
`order_size` ≈ `tgt_order_notional / price`. Share counts are
notional-driven at runtime, so rounded estimates are fine.

### Universal defaults from sym-population survey

Population survey across existing live traders (≥80% adoption unless
noted). Use these as defaults for any new sym that has no historical
backing yet; per-variant overrides below take precedence where they
collide.

| field | value | adoption |
|---|---|---|
| `pred_momentum_tdc` | `5` | 81% — short momentum lookback |
| `pred_momentum_coef` | `0.5` | 52% — moderate momentum tilt |
| `vol_norm_tdc` | `10` | 100% — universal |
| `vol_norm_coef` | `0.1` | 41% tied with 0.0; slight vol norm on |
| `curv_impulse_coef` | `0.0` | 78% — curv impulse OFF by default |
| `curv_impulse_tdc` | `30` | (only when `curv_impulse_coef` != 0; 6/6 of curv-on syms) |
| `premium_tdc_s` | `60` | 41% — middle of the road |
| `premium_ema_coef` | `0.9` | safe default, median |
| `per_order_widen_frac` | `0.5` | tied modal (22%) — also safer |
| `rung_spacing_mult` | `1.0` | 37% — modal |

### Systematic learnings to bake in (from live gf1 study)

These differ from the older TSM template values. Apply per variant:

**`pk_<sym>_usday.json`:**
| field | value |
|---|---|
| `premium_ema_coef` | `0.9` (matches survey) |
| `pred_momentum_tdc` | `5` (survey default) |
| `pred_momentum_coef` | `0.5` (survey default) |
| `can_cross` | `false` |
| `max_back_levels` | `6` |
| `per_backlevel_rung_spacing_mult` | `2.5` |
| `per_order_widen_frac` | `0.5` (survey default) |
| `thresh_mult_minfv` | `1.5` |
| `place_thresh` | from §2 |

**`pk_<sym>_2usday.json`:**
Same as usday **except** preserve sharp ladder character:
| field | value |
|---|---|
| `max_back_levels` | `2` (sharp ladder) |
| `per_backlevel_rung_spacing_mult` | `1.5` |
| Otherwise inherit usday's tunings (premium_ema_coef=0.9, pred_momentum_tdc=5, pred_momentum_coef=0.5, per_order_widen_frac=0.5). | |

**`pk_<sym>_postusa.json`:**
Stay close to TSM template; only `place_thresh` updated from §2. No
systematic learnings applied (live postusa data shows little drift
from template defaults). Apply survey defaults where the template
field is absent.

**`pk_<sym>_rel_nq_allday.json`:**
Template doesn't include premium fields — add them:
| field | value |
|---|---|
| `premium_ema_coef` | `0.8` (rel_nq-specific override; survey is 0.9) |
| `premium_tdc_s` | `120` (rel_nq-specific override; survey is 60 — rel_nq runs slower) |
| `place_thresh` | from §2 (capped at `0.008`) |
| `relative_beta` | `1.0` (placeholder; tune later by regression vs. NQ) |

### Don't include
- `hardcoded_min_tick` — leave to default unless symbol is sub-$1
- `last_perm_snapshot_*` — those are stale comments from older confs
- `thresh_mult_minfv` on non-usday variants (only battle-tested in usday)

### Reconcile risk vs ordex maxpos (run after generating the confs)

The hand-estimated share counts from §3 (`max_pos`, `max_position`) are
just rough notional/price estimates. Before committing, reconcile them
against the live HL price with:

```bash
strat_main/tools/check_maxpos.py --inplace for_live/saved_strats/<sym>/*.json
```

`ordex` is the source of truth; the script rewrites `risk` to match:
- `risk.max_position` ← `ordex.max_pos` (when `tgt_maxpos_notional` is absent)
- `risk.max_notional` ← `ordex.tgt_maxpos_notional` (when present)
- When `tgt_maxpos_notional` is set, the runtime query also requires
  `risk.max_position >= tgt_maxpos_notional / mid_price`. The script
  fetches the mid from HL mainnet `allMids` and, if `risk.max_position`
  is too small, bumps it to `ceil(1.2 × implied)` for price-drift
  headroom. (This is why generated `max_position` values often grow,
  e.g. a $90 name's rel_nq `max_position` jumping from the §3 estimate.)

It edits the raw text in place (comments/formatting preserved). Drop
`--inplace` to write `.fixed` copies for inspection first.

## 4. Update `strat_main/util/symbolizer.py`

Four dicts. All entries use the bare uppercase ticker (no `xyz:` prefix
except in `relwide_remote_wiring`).

```python
# relwide_remote_wiring
"xyz:<SYM>": {
    "remote_symbol": "<SYM>",
    "remote_market": "TopBookEquity",
    "relative_symbol": "BTC",
},

# _US_EQUITIES (set)
"<SYM>",

# syms_to_market (dict)
"<SYM>": "TopBookEquity",

# syms_go_live_dates (dict)
"<SYM>": "YYYYMMDD",  # date the symbol becomes available in your data
```

## 5. Update `strat_main/tools/supabase_snapshots.py`

Add to `self.sym_pairs`:

```python
("<SYM>", "NQ"),
("<SYM>", "BTC"),
```

Same for `supabase_closing.py` if it has its own pair list (worth
double-checking).

## 6. Add to the data-logging cron sym list

File: `datalog/cron/sym_lists/hyperliquid.txt`

This is the list the recording cron subscribes to — if the symbol isn't
here, **no market data is logged** for it. Insert `xyz:<SYM>` in the
`xyz:` block, which is kept **alphabetically sorted** (e.g. `xyz:NOW`
goes between `xyz:NIFTY` and `xyz:NVDA`).

Note: a symbol sometimes shows up here already (added when it first
listed on HL for data capture) while still missing from the trading
registries above — presence here does **not** mean the rest is done.

## 7. Refresh `src/pktrade/util/secmaster_lib/HyperliquidSec.h`

This file is a **snapshot of HL's hip3 metadata + mids**. Don't edit by
hand — it'll be wrong (szDecimals/leverage/timestamps come from HL's
API, not pattern matching).

Run the regenerator (or its current equivalent):

```bash
/home/david/.venvs/v1/bin/python /tmp/regen_hyperliquidsec.py
```

It POSTs to `https://api.hyperliquid.xyz/info` for `meta` and `allMids`
with `dex=xyz`, then patches the bodies of `loadHyperliquidHip3Info`
and `loadHyperliquidHip3Mids` in place.

A symbol must be **listed on HL hip3** for the regen to pick it up.
If it's not yet listed, skip this step and re-run after listing.

## 8. Assign a primary machine

File: `for_live/syms_primary_machines.txt`

Symbols are bucketed under a host header (`gf0:`, `gf1:`, …). Add
`xyz:<SYM>` under the machine that will run the trader. Equities
currently live on the equity-heavy boxes (`gf2`, `gf3`); pick one with
spare capacity. The list within a bucket is **not** strictly sorted —
appending to the end of the chosen bucket is fine.

This choice drives **which host's feed config you edit in §8a**, so
decide the machine first.

## 8a. Update the deployed feed config (on the chosen host)

The live feed configs are `for_live/feeds/feed.<host>.cpp.json` (e.g.
`feed.gf3.cpp.json`). The older `strat_main/pyfeed/feed.<host>.json`
files are **stale** and not what runs — don't rely on them. Edit the
config for the host picked in §8, both the repo copy and the deployed
copy on the live host.

**Weekday/weekend split:** some hosts run a separate weekend config —
e.g. gf3 runs `feed.gf3.cpp.json` on weekdays and
`feed.gf3.cpp.weekend.json` on weekends. When the chosen host has a
`.weekend.json`, **edit both files**, or the symbol silently has no
feed on the days the other config is active.

Fields to update (in each config you edit):

- `subscriptions.Hyperliquid.symbols` — append `"xyz:<SYM>"` (the HL
  hip3 book the trader quotes on). Mirror the same add into
  `subscriptions.HyperliquidNode.symbols` if present.
- `subscriptions.DataBentoEquities.symbols` — append `"<SYM>"` (the
  remote equity feed). **Skip if the name isn't on public equity
  markets yet** — there's no SIP data to subscribe to. Mirror the same
  add into `subscriptions.DataBentoBoats.symbols` if present.
- `subscriptions.DataBentoCME.symbols` — confirm `"NQ"` is present
  (required for any rel_nq strategy; missing NQ silently kills
  `FinalTempo` firing → no orders, no error)

After editing, **restart pkmultifeed** on that host so subscriptions
pick up. Then sync the change back to the repo so it doesn't drift
further.

## 9. (Optional) Regenerate combined conffiles

If you operate combined multi-symbol pktraders, re-run the combiner
that merges per-symbol confs into the combined dirs:
`for_live/saved_strats/combined_new_equities/pk_<variant>.json`.

## 10. Verify

```bash
# Symbolizer wiring
/home/david/.venvs/v1/bin/python -c "
import sys; sys.path.insert(0, '/home/david/tradefi/retraded_4/overmind')
from strat_main.util import symbolizer
sym = '<SYM>'
print('us_equity:', symbolizer.is_us_equity(sym))
print('wiring:', symbolizer.get_relwide_remote_meta('xyz:'+sym))
print('market:', symbolizer.syms_to_market.get(sym))
print('go_live:', symbolizer.syms_go_live_dates.get(sym))
"

# JSON validity for the new conffiles
for f in for_live/saved_strats/<sym>/*.json; do
    /home/david/.venvs/v1/bin/python -c "import json; json.load(open('$f'))" || echo "FAIL: $f"
done
```

## 11. Live debugging cheatsheet (when orders aren't placing)

A few facts to remember:

- **`trade_carrier`'s `checking book states` snapshot will always show
  zeros for `TopBookEquity` and `TopBookCme` books**, even when data is
  flowing. Those code paths use `onTopBookQuote`, which doesn't update
  the fields the diagnostic prints. Don't panic.

- **The first thing to verify is that NQ data is actually flowing**.
  Without `feed.dispatch market=PBMARKET_TOPBOOK_CME sym=NQ` lines,
  `FinalTempo::onFinal` never fires and no orders are ever sent. This
  bit us when adding the new symbols on a host that hadn't subscribed
  to NQ.

- **A working ZMQ SUB on the same socket should always see traffic**.
  If you suspect the trader isn't connecting, run
  `python3 /tmp/zmq_sniff.py ipc:///tmp/feed-sock` to confirm
  pkmultifeed is publishing.

- **Topic mismatch is silent**. ZMQ pub/sub filters on byte-prefix of
  frame 1; the trader subscribes with `""` (all topics) per `feed.cc`,
  so subscription mismatch shouldn't be a failure mode unless that
  changes.

- **Hyperliquid `xyz:<SYM>` book may show `seen_snapshot 1` with zero
  updates** if HL has the listing but no one is actively quoting the
  symbol. That's not the trader's fault.
