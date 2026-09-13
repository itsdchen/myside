# CRWV — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:CRWV`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**6 shocks: 2 earnings-linked, 4 other.**

---

# Earnings

## 2026-02-26 17:40 ET  (postmarket)

**EARNINGS 2026-02-26**  ·  HL perp move +3.60% at 9.8σ on $156,377 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-25) = $15,350,694 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:30 | 87.21 | — | +0.00% | $24,813,717 | 287,036 | 1.62x |
| 17:35 | 86.68 | -0.61% | -0.61% | $14,306,195 | 165,121 | 0.93x |
| **17:40** | **90.26** | **+4.13%** | **+3.50%** | **$18,645,147** | **209,414** | **1.21x** |
| 17:45 | 88.83 | -1.58% | +1.86% | $6,542,599 | 73,477 | 0.43x |
| 17:50 | 88.00 | -0.93% | +0.91% | $22,179,153 | 252,722 | 1.44x |


## 2026-05-07 16:10 ET  (postmarket)

**EARNINGS 2026-05-07**  ·  HL perp move -5.67% at 16.2σ on $2,495,428 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-06) = $33,589,579 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:00 | 129.71 | — | +0.00% | $22,625,048 | 173,461 | 0.67x |
| 16:05 | 131.42 | +1.32% | +1.32% | $50,018,042 | 378,001 | 1.49x |
| **16:10** | **123.50** | **-6.03%** | **-4.79%** | **$56,579,947** | **454,415** | **1.68x** |
| 16:15 | 126.29 | +2.26% | -2.64% | $34,509,199 | 277,856 | 1.03x |
| 16:20 | 128.22 | +1.53% | -1.15% | $28,911,273 | 225,139 | 0.86x |


---

# Other (non-earnings)

## 2026-02-08 20:05 ET  (overnight)

HL perp move +2.23% at 8.3σ on $65,466 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-02-06) = $23,338,392 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| 20:00 | 92.30 | — | — | $1,348,180 | 14,737 | 0.06x |
| **20:05** | **92.92** | **+0.67%** | **—** | **$871,376** | **9,402** | **0.04x** |
| 20:10 | 92.86 | -0.06% | — | $786,861 | 8,459 | 0.03x |
| 20:15 | 92.62 | -0.26% | — | $410,578 | 4,427 | 0.02x |


## 2026-03-16 06:05 ET  (premarket)

HL perp move +2.61% at 7.2σ on $65,070 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-13) = $11,928,601 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 05:55 | 82.29 | — | +0.00% | $5,760 | 70 | 0.00x |
| 06:00 | 84.50 | +2.69% | +2.69% | $1,509,427 | 17,947 | 0.13x |
| **06:05** | **86.47** | **+2.33%** | **+5.08%** | **$2,229,696** | **25,979** | **0.19x** |
| 06:10 | 85.72 | -0.87% | +4.17% | $781,367 | 9,104 | 0.07x |
| 06:15 | 85.65 | -0.08% | +4.08% | $801,663 | 9,384 | 0.07x |


## 2026-07-01 08:35 ET  (premarket)

HL perp move -5.22% at 11.2σ on $1,263,105 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-30) = $13,696,093 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:25 | 99.91 | — | +0.00% | $406,667 | 4,067 | 0.03x |
| 08:30 | 96.00 | -3.91% | -3.91% | $7,579,323 | 77,324 | 0.55x |
| **08:35** | **93.07** | **-3.05%** | **-6.84%** | **$10,037,248** | **106,462** | **0.73x** |
| 08:40 | 94.25 | +1.27% | -5.66% | $8,653,319 | 91,639 | 0.63x |
| 08:45 | 91.17 | -3.27% | -8.74% | $17,276,598 | 187,272 | 1.26x |


## 2026-07-22 17:00 ET  (postmarket)

HL perp move +3.10% at 8.4σ on $116,134 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $14,603,452 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:50 | 82.80 | — | +0.00% | $216,154 | 2,612 | 0.01x |
| 16:55 | 83.93 | +1.37% | +1.37% | $1,600,953 | 19,014 | 0.11x |
| **17:00** | **86.51** | **+3.07%** | **+4.48%** | **$7,487,082** | **87,150** | **0.51x** |
| 17:05 | 86.07 | -0.51% | +3.95% | $4,618,295 | 53,725 | 0.32x |
| 17:10 | 85.01 | -1.23% | +2.67% | $3,424,728 | 39,880 | 0.23x |

