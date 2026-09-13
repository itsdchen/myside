# hip3 shock volume study — summary

Generated 2026-07-29. **203 shocks across 45 symbols**, detected on the Hyperliquid
perp tape and profiled against the US equity tape that was actually open.

Per-symbol tables are in `<TICKER>.md`; flat per-bucket data in `all_buckets.csv`.

---

## Method in brief

**Detection (Hyperliquid).** 5-minute buckets on the HL trade tape. A bucket is a
shock when it clears *all* of: ≥5σ, ≥2% absolute move, and ≥5 trades / ≥$25k
notional on both sides of the return. σ is a per-session 16–84th percentile
half-range over traded buckets only, floored at 5bp — a plain stdev would be
inflated by the very shocks we're hunting, and MAD collapses to exactly zero on
these symbols. Consecutive flagged buckets merge into one event.

**Extended hours only.** Nothing between 09:30 and 16:00 is flagged. Cash-session
moves are dominated by the open gapping the perp, which is a different phenomenon
from a headline hitting a quiet tape.

**Profiling (US equities).** Five 5-minute buckets centred on each shock, from
whichever venue was trading: `XNAS.BASIC` 04:00–20:00 ET, `OCEA.MEMOIR`
(Blue Ocean ATS) 20:00–04:00. Notional = Σ (volume × close) per 1-min bar.

**Baseline.** Previous trading day's Nasdaq regular session, 09:32–15:58, averaged
per 5-min bucket. It's the same yardstick for every event on either venue, so
`vs base` always means "relative to a normal daytime five minutes". The 3 minutes
centred on 09:30 and 16:00 are excluded everywhere — the crosses are enormous and
are not continuous trading.

**Earnings marking.** Against a backfilled Nasdaq calendar (158 dates, 48 tickers).
Nasdaq strips the AMC/BMO marker from historical rows, so the tag covers any shock
in the window a report on date D could move: D 16:00 → D+1 16:00, or D 00:00 → D 16:00.
A single print often trips the detector several times, so each (symbol, earnings
date) is collapsed to its **highest-volume** event.

---

## Composition

| | Events | Share |
|---|---:|---:|
| Total | 203 | |
| Earnings-linked | 43 | 21% |
| Not earnings | 160 | **79%** |

| Session | Events | | Venue | Events |
|---|---:|---|---|---:|
| Postmarket | 73 | | Nasdaq Basic | 136 |
| Overnight | 67 | | Blue Ocean | 67 |
| Premarket | 63 | | | |

---

## Finding — volume response is wildly disproportionate to the move

Earnings shocks move somewhat further than non-earnings shocks, but they trade
far more than that difference explains:

| | Median move | Median volume, postmarket (vs baseline) |
|---|---:|---:|
| Earnings | 3.25% | **3.23×** |
| Not earnings | 2.42% | **0.57×** |

The move is **1.3×** larger. The volume is **5.7×** larger.

A non-earnings shock repricing a stock 2.4% does so on roughly half a normal
daytime five minutes of volume. These are thin-tape repricings: the price gets
marked, very little changes hands.

Volume multiple at the shock bucket, by session:

| Session | Earnings | n | Other | n |
|---|---:|---:|---:|---:|
| Postmarket | 3.23× | 37 | 0.57× | 36 |
| Premarket | 0.75× | 6 | 0.39× | 57 |
| Overnight | — | 0 | 0.14× | 67 |

Only postmarket earnings clears the baseline. Everything else trades below a
normal daytime bucket, and overnight non-earnings shocks run at **14%** of one.

Median notional actually traded in the shock bucket:

| Postmarket | Premarket | Overnight |
|---:|---:|---:|
| $43.7M | $13.2M | $7.0M |

Note also that 79% of ≥2% extended-hours shocks have no earnings date near them,
so the thin-tape case is the common one, not the exception.

---

## Largest events

By volume response (`vs base` at the shock bucket):

| Symbol | Time (ET) | Session | Earnings | Move | Notional | vs base |
|---|---|---|---|---:|---:|---:|
| PLTR | 2026-02-02 16:05 | postmarket | yes | +6.66% | $479.4M | 9.23× |
| GOOGL | 2026-02-04 16:00 | postmarket | yes | −0.07% | $582.6M | 7.29× |
| ORCL | 2026-03-10 16:05 | postmarket | yes | +5.24% | $221.2M | 7.21× |
| META | 2025-12-04 09:05 | premarket | **no** | +1.55% | $280.9M | 6.26× |
| NOW | 2026-07-22 16:15 | postmarket | yes | +5.17% | $71.9M | 6.02× |
| AMZN | 2026-02-05 16:00 | postmarket | yes | −10.25% | $499.9M | 5.96× |

By price move:

| Symbol | Time (ET) | Earnings | Move | Notional | vs base |
|---|---|---|---:|---:|---:|
| INTC | 2026-04-23 16:00 | yes | **+17.07%** | $144.9M | 3.35× |
| AMZN | 2026-02-05 16:00 | yes | −10.25% | $499.9M | 5.96× |
| INTC | 2026-07-23 16:00 | yes | +8.54% | $292.6M | 4.92× |
| RIVN | 2026-02-12 16:00 | yes | +8.37% | $9.3M | 3.35× |
| HOOD | 2026-02-10 16:05 | yes | −7.65% | $99.1M | 3.79× |
| MSFT | 2026-01-28 16:00 | yes | −7.14% | $206.1M | 2.24× |

Every one of the largest moves is an earnings print. The largest volume responses
are also mostly earnings — with META 2025-12-04 the notable exception, a
non-earnings premarket event at 6.26× baseline on $280.9M.

---

## Data coverage — read this before trusting any per-symbol count

**A symbol having few earnings events here usually means we lacked the tape, not
that the stock didn't move.** Of the 158 earnings dates in the hip3 universe:

| | Dates |
|---|---:|
| Covered by local HL data | 58 |
| Before first capture for that symbol (pre-listing) | **84** |
| Gap inside the capture range | 8 |
| Symbol never captured (GEV, PURR) | 7 |
| After last capture | 1 |

Where we *do* have tape we detect 43 of 58 (74%); only **15** covered earnings
dates produced no ≥2% shock and are genuine small moves. So the per-symbol counts
in `README.md` are a coverage map at least as much as a volatility map — AAPL
shows one earnings event not because its prints are quiet, but because one date
predates our capture and another falls in a gap.

Three separate causes, worth keeping distinct:

1. **Pre-listing (84 dates).** hip3 listings rolled out through late 2025 and 2026;
   before a symbol was listed there is no perp and nothing to detect. Unfixable.
2. **Live capture outage (~8 dates).** 2026-04-28 → 04-30 is missing across many
   symbols simultaneously (GOOGL, META, MSFT, HOOD, LLY, RIVN, SNDK). Those
   objects are absent from S3 entirely, so the recorder was down — during the
   heaviest week of Q1 earnings season. Unrecoverable.
3. **Un-downloaded files (123 trades files).** These exist in S3 but were never
   pulled locally, despite the backfill reporting success for every symbol. AAPL,
   AMD, COIN and CRCL are each short 7 files; most other symbols short 1.
   **Recoverable** — and worth doing before any follow-up study.

---

## Status

This is a first pass, not a finished result. It establishes the method and gives a
usable read on 203 shocks, but the coverage gaps above mean the sample is smaller
and more uneven than it could be. A more comprehensive study should:

- backfill the 123 recoverable files, then re-scan
- replace the single-day baseline with a trailing 10-session median
- re-run detection at the 2% floor properly rather than filtering the 1% results
- measure lead-lag between the HL perp and the equity tape, which both feeds are
  already aligned for

---

## Caveats

- **Dropped events.** From 274 filtered shocks: 27 had no open US venue
  (weekend / Fri+Sat night, when Blue Ocean is dark), 23 returned no bars (market
  holiday, or name not yet listed), and 21 were same-earnings-date repeats.
  203 remain.
- **Single-day baseline.** The denominator is one prior session, averaged over
  ~78 buckets so reasonably stable, but a trailing 10-session median would be
  better and costs nothing but runtime.
