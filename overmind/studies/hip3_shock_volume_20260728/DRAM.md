# DRAM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:DRAM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**9 shocks: 0 earnings-linked, 9 other.**

---

# Other (non-earnings)

## 2026-06-01 20:05 ET  (overnight)

HL perp move -2.62% at 8.9σ on $563,392 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $11,178,742 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| 20:00 | 67.86 | — | — | $2,200,200 | 32,432 | 0.20x |
| **20:05** | **66.22** | **-2.42%** | **—** | **$7,219,992** | **108,603** | **0.65x** |
| 20:10 | 67.00 | +1.18% | — | $4,298,998 | 64,274 | 0.38x |
| 20:15 | 66.19 | -1.21% | — | $3,696,139 | 55,606 | 0.33x |


## 2026-06-07 20:00 ET  (overnight)

HL perp move +3.06% at 9.2σ on $731,558 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-05) = $28,119,067 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **57.86** | **—** | **—** | **$6,421,410** | **111,444** | **0.23x** |
| 20:05 | 56.80 | -1.83% | — | $7,749,504 | 134,946 | 0.28x |
| 20:10 | 57.83 | +1.81% | — | $3,828,874 | 66,501 | 0.14x |


## 2026-06-07 20:35 ET  (overnight)

HL perp move +1.16% at 8.9σ on $410,925 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-05) = $28,119,067 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:25 | 57.93 | — | +0.00% | $1,634,479 | 28,254 | 0.06x |
| 20:30 | 57.79 | -0.24% | -0.24% | $2,816,878 | 48,760 | 0.10x |
| **20:35** | **59.38** | **+2.75%** | **+2.50%** | **$12,072,643** | **203,735** | **0.43x** |
| 20:40 | 58.43 | -1.60% | +0.86% | $4,292,730 | 73,269 | 0.15x |
| 20:45 | 58.24 | -0.33% | +0.54% | $3,301,962 | 56,879 | 0.12x |


## 2026-06-10 08:30 ET  (premarket)

HL perp move +3.58% at 16.4σ on $616,494 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-09) = $25,005,370 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 56.93 | — | +0.00% | $746,996 | 13,101 | 0.03x |
| 08:25 | 57.44 | +0.90% | +0.90% | $1,654,561 | 28,913 | 0.07x |
| **08:30** | **59.52** | **+3.62%** | **+4.55%** | **$14,839,648** | **251,441** | **0.59x** |
| 08:35 | 59.29 | -0.39% | +4.14% | $5,238,491 | 88,481 | 0.21x |
| 08:40 | 59.00 | -0.48% | +3.64% | $6,413,201 | 109,113 | 0.26x |


## 2026-06-22 20:20 ET  (overnight)

HL perp move -2.39% at 8.1σ on $1,488,024 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-18) = $23,283,346 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:10 | 79.43 | — | +0.00% | $2,535,670 | 31,842 | 0.11x |
| 20:15 | 79.18 | -0.31% | -0.31% | $7,521,838 | 94,830 | 0.32x |
| **20:20** | **77.21** | **-2.49%** | **-2.79%** | **$4,573,602** | **58,680** | **0.20x** |
| 20:25 | 78.14 | +1.20% | -1.62% | $5,938,741 | 76,194 | 0.26x |
| 20:30 | 78.39 | +0.32% | -1.31% | $3,347,536 | 42,705 | 0.14x |


## 2026-06-24 16:00 ET  (postmarket)

HL perp move +8.34% at 20.5σ on $11,527,352 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $30,871,391 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 69.30 | — | +0.00% | $33,219,146 | 480,004 | 1.08x |
| 15:55 | 69.66 | +0.51% | +0.51% | $36,450,767 | 524,586 | 1.18x |
| **16:00** | **72.90** | **+4.66%** | **+5.19%** | **$18,648,702** | **258,559** | **0.60x** |
| 16:05 | 75.90 | +4.11% | +9.52% | $32,178,052 | 430,789 | 1.04x |
| 16:10 | 75.63 | -0.35% | +9.13% | $28,420,830 | 377,856 | 0.92x |


## 2026-06-24 16:20 ET  (postmarket)

HL perp move +4.52% at 15.4σ on $10,570,877 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $30,871,391 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:10 | 75.63 | — | +0.00% | $28,420,830 | 377,856 | 0.92x |
| 16:15 | 75.64 | +0.01% | +0.01% | $29,634,532 | 393,282 | 0.96x |
| **16:20** | **78.05** | **+3.19%** | **+3.20%** | **$28,430,349** | **368,425** | **0.92x** |
| 16:25 | 79.19 | +1.46% | +4.71% | $35,313,658 | 447,213 | 1.14x |
| 16:30 | 76.97 | -2.80% | +1.77% | $40,526,632 | 520,219 | 1.31x |


## 2026-07-02 21:05 ET  (overnight)

HL perp move +2.05% at 6.8σ on $203,996 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-07-01) = $25,395,354 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:55 | — | — | — | $0 | 0 | 0.00x |
| 21:00 | — | — | — | $0 | 0 | 0.00x |
| **21:05** | **—** | **—** | **—** | **$0** | **0** | **0.00x** |
| 21:10 | — | — | — | $0 | 0 | 0.00x |
| 21:15 | — | — | — | $0 | 0 | 0.00x |


## 2026-07-14 08:30 ET  (premarket)

HL perp move +2.09% at 9.6σ on $4,247,067 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-13) = $24,179,548 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 59.54 | — | +0.00% | $1,307,714 | 22,009 | 0.05x |
| 08:25 | 59.58 | +0.06% | +0.06% | $3,186,681 | 53,497 | 0.13x |
| **08:30** | **60.91** | **+2.23%** | **+2.30%** | **$11,624,271** | **191,231** | **0.48x** |
| 08:35 | 60.41 | -0.81% | +1.47% | $9,173,272 | 151,692 | 0.38x |
| 08:40 | 60.66 | +0.41% | +1.88% | $6,538,726 | 107,756 | 0.27x |

