# PURR — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:PURR`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**7 shocks: 0 earnings-linked, 7 other.**

---

# Other (non-earnings)

## 2026-05-15 08:45 ET  (premarket)

HL perp move -4.31% at 8.4σ on $147,748 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-14) = $1,008,247 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:35 | 7.04 | — | +0.00% | $39,037 | 5,547 | 0.04x |
| 08:40 | 6.88 | -2.27% | -2.27% | $161,366 | 23,437 | 0.16x |
| **08:45** | **6.73** | **-2.18%** | **-4.40%** | **$195,911** | **28,977** | **0.19x** |
| 08:50 | 6.75 | +0.30% | -4.12% | $136,939 | 20,531 | 0.14x |
| 08:55 | 6.78 | +0.49% | -3.65% | $8,351 | 1,242 | 0.01x |


## 2026-05-18 17:15 ET  (postmarket)

HL perp move +8.69% at 12.5σ on $580,760 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-15) = $552,814 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:05 | 6.98 | — | +0.00% | $1,075 | 154 | 0.00x |
| 17:10 | 7.22 | +3.44% | +3.44% | $634,468 | 87,689 | 1.15x |
| **17:15** | **7.51** | **+4.02%** | **+7.59%** | **$91,558** | **12,375** | **0.17x** |
| 17:20 | 7.93 | +5.59% | +13.61% | $1,276,241 | 158,526 | 2.31x |
| 17:25 | 7.80 | -1.64% | +11.75% | $1,137,838 | 144,302 | 2.06x |


## 2026-05-31 20:55 ET  (overnight)

HL perp move -3.12% at 5.3σ on $365,454 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $2,520,825 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:45 | 12.00 | — | +0.00% | $660 | 55 | 0.00x |
| 20:50 | 12.00 | +0.00% | +0.00% | $252 | 21 | 0.00x |
| **20:55** | **11.84** | **-1.33%** | **-1.33%** | **$260,789** | **22,170** | **0.10x** |
| 21:00 | 12.00 | +1.35% | +0.00% | $18,461 | 1,542 | 0.01x |
| 21:05 | 11.95 | -0.42% | -0.42% | $67,271 | 5,630 | 0.03x |


## 2026-06-01 09:25 ET  (premarket)

HL perp move -2.68% at 5.2σ on $113,604 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $2,520,825 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 11.99 | — | +0.00% | $295,243 | 24,680 | 0.12x |
| 09:20 | 11.95 | -0.33% | -0.33% | $323,750 | 27,082 | 0.13x |
| **09:25** | **11.70** | **-2.09%** | **-2.42%** | **$648,316** | **54,952** | **0.26x** |
| 09:30 | 10.91 | -6.71% | -8.97% | $13,677,286 | 1,243,574 | 5.43x |
| 09:35 | 10.96 | +0.37% | -8.63% | $11,228,331 | 1,023,688 | 4.45x |


## 2026-06-04 03:20 ET  (overnight)

HL perp move -3.61% at 11.7σ on $1,419,383 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-03) = $1,685,755 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 03:10 | — | — | — | $0 | 0 | 0.00x |
| 03:15 | 10.02 | — | — | $19,957 | 1,991 | 0.01x |
| **03:20** | **9.80** | **-2.20%** | **—** | **$118,644** | **12,055** | **0.07x** |
| 03:25 | 9.77 | -0.31% | — | $401,541 | 41,222 | 0.24x |
| 03:30 | 9.76 | -0.10% | — | $43,063 | 4,467 | 0.03x |


## 2026-06-04 09:25 ET  (premarket)

HL perp move +3.24% at 6.1σ on $278,713 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-03) = $1,685,755 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 9.51 | — | +0.00% | $319,893 | 33,484 | 0.19x |
| 09:20 | 9.48 | -0.32% | -0.32% | $201,357 | 21,295 | 0.12x |
| **09:25** | **9.71** | **+2.43%** | **+2.10%** | **$479,249** | **49,643** | **0.28x** |
| 09:30 | 9.82 | +1.18% | +3.31% | $3,279,216 | 337,115 | 1.95x |
| 09:35 | 9.90 | +0.76% | +4.10% | $8,249,235 | 840,734 | 4.89x |


## 2026-06-23 04:20 ET  (premarket)

HL perp move +3.16% at 6.0σ on $978,419 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-22) = $733,564 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 04:10 | 8.55 | — | +0.00% | $966 | 113 | 0.00x |
| 04:15 | 8.27 | -3.27% | -3.27% | $2,689 | 325 | 0.00x |
| **04:20** | **8.35** | **+0.97%** | **-2.34%** | **$236,743** | **28,295** | **0.32x** |
| 04:25 | 8.37 | +0.24% | -2.11% | $6,600 | 783 | 0.01x |
| 04:30 | 8.40 | +0.36% | -1.75% | $377,937 | 45,290 | 0.52x |

