# HOOD — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:HOOD`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**5 shocks: 1 earnings-linked, 4 other.**

---

# Earnings

## 2026-02-10 16:05 ET  (postmarket)

**EARNINGS 2026-02-10**  ·  HL perp move -7.67% at 44.0σ on $1,778,889 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-09) = $26,133,468 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 85.64 | — | +0.00% | $59,111,231 | 690,236 | 2.26x |
| 16:00 | 86.30 | +0.77% | +0.77% | $21,065,188 | 244,669 | 0.81x |
| **16:05** | **79.70** | **-7.65%** | **-6.94%** | **$99,120,860** | **1,229,699** | **3.79x** |
| 16:10 | 79.97 | +0.34% | -6.62% | $70,485,157 | 895,392 | 2.70x |
| 16:15 | 80.68 | +0.89% | -5.79% | $41,016,255 | 512,691 | 1.57x |


---

# Other (non-earnings)

## 2026-01-29 16:00 ET  (postmarket)

HL perp move +4.24% at 22.9σ on $1,856,467 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-28) = $19,041,010 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 100.64 | — | +0.00% | $28,961,964 | 287,888 | 1.52x |
| 15:55 | 101.25 | +0.60% | +0.60% | $42,484,558 | 419,736 | 2.23x |
| **16:00** | **105.21** | **+3.92%** | **+4.54%** | **$40,220,366** | **382,597** | **2.11x** |
| 16:05 | 104.82 | -0.37% | +4.15% | $42,887,423 | 410,608 | 2.25x |
| 16:10 | 104.31 | -0.49% | +3.64% | $5,912,848 | 56,772 | 0.31x |


## 2026-03-23 07:05 ET  (premarket)

HL perp move +4.30% at 18.0σ on $365,509 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-20) = $15,642,516 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 68.58 | — | +0.00% | $114,949 | 1,673 | 0.01x |
| 07:00 | 68.52 | -0.09% | -0.09% | $187,290 | 2,728 | 0.01x |
| **07:05** | **71.50** | **+4.35%** | **+4.26%** | **$1,147,124** | **16,181** | **0.07x** |
| 07:10 | 72.51 | +1.41% | +5.73% | $1,731,555 | 24,061 | 0.11x |
| 07:15 | 71.44 | -1.48% | +4.17% | $1,172,462 | 16,321 | 0.07x |


## 2026-04-07 18:35 ET  (postmarket)

HL perp move +2.22% at 12.1σ on $409,117 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-04-06) = $14,578,098 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 18:25 | 70.90 | — | +0.00% | $510,619 | 7,201 | 0.04x |
| 18:30 | 71.32 | +0.59% | +0.59% | $2,601,072 | 36,662 | 0.18x |
| **18:35** | **72.82** | **+2.11%** | **+2.71%** | **$5,948,943** | **82,038** | **0.41x** |
| 18:40 | 72.52 | -0.41% | +2.29% | $3,581,562 | 49,233 | 0.25x |
| 18:45 | 72.56 | +0.05% | +2.35% | $3,178,239 | 43,787 | 0.22x |


## 2026-06-23 09:25 ET  (premarket)

HL perp move +2.04% at 8.6σ on $788,136 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-22) = $26,615,606 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 99.14 | — | +0.00% | $11,355,118 | 114,536 | 0.43x |
| 09:20 | 99.65 | +0.51% | +0.51% | $11,015,708 | 110,725 | 0.41x |
| **09:25** | **100.63** | **+0.99%** | **+1.50%** | **$3,825,569** | **38,280** | **0.14x** |
| 09:30 | 101.75 | +1.11% | +2.63% | $45,245,738 | 441,625 | 1.70x |
| 09:35 | 102.62 | +0.86% | +3.51% | $64,995,484 | 636,901 | 2.44x |

