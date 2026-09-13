# NBIS — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:NBIS`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**5 shocks: 0 earnings-linked, 5 other.**

---

# Other (non-earnings)

## 2026-06-24 16:05 ET  (postmarket)

HL perp move +1.42% at 11.1σ on $981,020 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $42,208,153 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 259.44 | — | +0.00% | $95,425,183 | 368,108 | 2.26x |
| 16:00 | 263.50 | +1.56% | +1.56% | $6,604,415 | 25,037 | 0.16x |
| **16:05** | **272.50** | **+3.42%** | **+5.03%** | **$31,501,488** | **117,152** | **0.75x** |
| 16:10 | 267.00 | -2.02% | +2.91% | $16,401,067 | 61,143 | 0.39x |
| 16:15 | 267.50 | +0.19% | +3.11% | $4,668,039 | 17,418 | 0.11x |


## 2026-07-01 08:45 ET  (premarket)

HL perp move -4.06% at 6.2σ on $3,132,122 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-30) = $28,323,379 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:35 | 261.97 | — | +0.00% | $12,571,573 | 47,986 | 0.44x |
| 08:40 | 260.01 | -0.75% | -0.75% | $21,472,947 | 82,665 | 0.76x |
| **08:45** | **254.72** | **-2.03%** | **-2.77%** | **$19,063,110** | **73,882** | **0.67x** |
| 08:50 | 249.40 | -2.09% | -4.80% | $37,175,235 | 147,876 | 1.31x |
| 08:55 | 249.05 | -0.14% | -4.93% | $27,432,909 | 109,508 | 0.97x |


## 2026-07-01 09:25 ET  (premarket)

HL perp move -2.72% at 7.5σ on $1,919,489 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-30) = $28,323,379 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 247.10 | — | +0.00% | $22,665,548 | 90,987 | 0.80x |
| 09:20 | 247.75 | +0.26% | +0.26% | $28,727,130 | 115,546 | 1.01x |
| **09:25** | **240.91** | **-2.76%** | **-2.51%** | **$41,267,377** | **170,261** | **1.46x** |
| 09:30 | 228.75 | -5.05% | -7.43% | $181,437,425 | 782,884 | 6.41x |
| 09:35 | 237.50 | +3.83% | -3.89% | $284,515,114 | 1,207,044 | 10.05x |


## 2026-07-22 16:10 ET  (postmarket)

HL perp move +2.07% at 6.6σ on $79,565 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $36,357,759 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:00 | 217.52 | — | +0.00% | $1,653,975 | 7,594 | 0.05x |
| 16:05 | 216.75 | -0.35% | -0.35% | $8,848,534 | 40,972 | 0.24x |
| **16:10** | **220.80** | **+1.87%** | **+1.51%** | **$7,252,189** | **33,021** | **0.20x** |
| 16:15 | 223.39 | +1.17% | +2.70% | $15,434,624 | 69,344 | 0.42x |
| 16:20 | 223.35 | -0.02% | +2.68% | $5,923,844 | 26,584 | 0.16x |


## 2026-07-22 17:00 ET  (postmarket)

HL perp move +2.76% at 8.8σ on $1,153,200 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $36,357,759 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:50 | 221.75 | — | +0.00% | $1,248,232 | 5,638 | 0.03x |
| 16:55 | 225.84 | +1.84% | +1.84% | $10,278,002 | 45,529 | 0.28x |
| **17:00** | **231.01** | **+2.29%** | **+4.18%** | **$19,856,241** | **86,658** | **0.55x** |
| 17:05 | 231.00 | -0.00% | +4.17% | $13,561,765 | 59,020 | 0.37x |
| 17:10 | 228.00 | -1.30% | +2.82% | $6,275,658 | 27,439 | 0.17x |

