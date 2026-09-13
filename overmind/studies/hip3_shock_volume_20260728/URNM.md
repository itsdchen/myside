# URNM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:URNM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 0 earnings-linked, 3 other.**

---

# Other (non-earnings)

## 2026-02-12 09:15 ET  (premarket)

HL perp move +3.31% at 11.9σ on $30,122 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-11) = $439,680 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:05 | — | — | — | $0 | 0 | 0.00x |
| 09:10 | — | — | — | $0 | 0 | 0.00x |
| **09:15** | **72.16** | **—** | **—** | **$21,071** | **292** | **0.05x** |
| 09:20 | 72.41 | +0.35% | — | $72 | 1 | 0.00x |
| 09:25 | — | — | — | $0 | 0 | 0.00x |


## 2026-03-08 20:40 ET  (overnight)

HL perp move -5.88% at 6.1σ on $429,972 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-06) = $300,101 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:30 | 61.94 | — | +0.00% | $9,882 | 159 | 0.03x |
| 20:35 | 61.91 | -0.05% | -0.05% | $994 | 16 | 0.00x |
| **20:40** | **60.57** | **-2.16%** | **-2.21%** | **$19,000** | **313** | **0.06x** |
| 20:45 | 59.03 | -2.54% | -4.70% | $75,072 | 1,247 | 0.25x |
| 20:50 | 60.57 | +2.61% | -2.21% | $40,452 | 670 | 0.13x |


## 2026-03-18 20:20 ET  (overnight)

HL perp move -3.58% at 7.3σ on $62,721 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-17) = $240,039 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:10 | 61.44 | — | +0.00% | $14,254 | 232 | 0.06x |
| 20:15 | 60.84 | -0.98% | -0.98% | $39,809 | 654 | 0.17x |
| **20:20** | **61.01** | **+0.28%** | **-0.70%** | **$61** | **1** | **0.00x** |
| 20:25 | 61.57 | +0.92% | +0.21% | $62 | 1 | 0.00x |
| 20:30 | — | — | — | $0 | 0 | 0.00x |

