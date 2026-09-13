# EWY — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:EWY`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**9 shocks: 0 earnings-linked, 9 other.**

---

# Other (non-earnings)

## 2026-03-04 01:25 ET  (overnight)

HL perp move +2.37% at 7.8σ on $255,394 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-03) = $42,370,628 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 01:15 | 120.28 | — | +0.00% | $7,646,259 | 63,320 | 0.18x |
| 01:20 | 120.34 | +0.05% | +0.05% | $3,615,935 | 30,071 | 0.09x |
| **01:25** | **121.40** | **+0.88%** | **+0.93%** | **$2,663,643** | **22,010** | **0.06x** |
| 01:30 | 122.50 | +0.91% | +1.85% | $3,340,468 | 27,326 | 0.08x |
| 01:35 | 123.01 | +0.42% | +2.27% | $2,324,637 | 18,858 | 0.05x |


## 2026-03-04 19:05 ET  (postmarket)

HL perp move -0.99% at 15.2σ on $162,056 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-03) = $42,370,628 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 18:55 | 135.95 | — | +0.00% | $1,274,820 | 9,379 | 0.03x |
| 19:00 | — | — | — | $0 | 0 | 0.00x |
| **19:05** | **—** | **—** | **—** | **$0** | **0** | **0.00x** |
| 19:10 | — | — | — | $0 | 0 | 0.00x |
| 19:15 | — | — | — | $0 | 0 | 0.00x |


## 2026-04-01 21:15 ET  (overnight)

HL perp move -2.37% at 8.0σ on $434,878 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-31) = $21,243,767 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 21:05 | 125.90 | — | +0.00% | $803,217 | 6,422 | 0.04x |
| 21:10 | 126.50 | +0.48% | +0.48% | $1,193,562 | 9,412 | 0.06x |
| **21:15** | **123.23** | **-2.58%** | **-2.12%** | **$4,574,285** | **36,883** | **0.22x** |
| 21:20 | 122.60 | -0.51% | -2.62% | $2,894,355 | 23,532 | 0.14x |
| 21:25 | 122.45 | -0.12% | -2.74% | $4,686,465 | 38,435 | 0.22x |


## 2026-04-07 18:35 ET  (postmarket)

HL perp move +3.24% at 17.7σ on $1,002,778 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-04-06) = $5,626,472 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 18:25 | 129.66 | — | +0.00% | $311,558 | 2,404 | 0.06x |
| 18:30 | 130.64 | +0.76% | +0.76% | $2,608,414 | 20,253 | 0.46x |
| **18:35** | **135.50** | **+3.72%** | **+4.50%** | **$5,689,574** | **42,450** | **1.01x** |
| 18:40 | 134.41 | -0.80% | +3.66% | $2,649,273 | 19,804 | 0.47x |
| 18:45 | 133.70 | -0.53% | +3.12% | $1,156,640 | 8,669 | 0.21x |


## 2026-05-11 21:30 ET  (overnight)

HL perp move -2.10% at 7.5σ on $1,868,166 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-08) = $17,732,060 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 21:20 | 186.73 | — | +0.00% | $1,774,271 | 9,492 | 0.10x |
| 21:25 | 185.90 | -0.44% | -0.44% | $3,341,079 | 17,983 | 0.19x |
| **21:30** | **181.22** | **-2.52%** | **-2.95%** | **$3,841,054** | **21,045** | **0.22x** |
| 21:35 | 178.65 | -1.42% | -4.33% | $14,245,744 | 79,330 | 0.80x |
| 21:40 | 181.62 | +1.66% | -2.74% | $13,553,809 | 75,216 | 0.76x |


## 2026-06-01 20:05 ET  (overnight)

HL perp move -3.26% at 10.5σ on $2,353,630 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $12,287,944 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| 20:00 | 213.97 | — | — | $11,524,621 | 53,807 | 0.94x |
| **20:05** | **207.50** | **-3.02%** | **—** | **$10,561,428** | **50,268** | **0.86x** |
| 20:10 | 210.94 | +1.66% | — | $12,234,661 | 58,425 | 1.00x |
| 20:15 | 207.21 | -1.77% | — | $5,224,329 | 25,121 | 0.43x |


## 2026-06-09 20:00 ET  (overnight)

HL perp move +2.62% at 8.7σ on $1,180,530 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-08) = $20,259,419 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **189.18** | **—** | **—** | **$17,415,662** | **92,842** | **0.86x** |
| 20:05 | 190.20 | +0.54% | — | $12,775,199 | 67,291 | 0.63x |
| 20:10 | 189.81 | -0.21% | — | $4,572,245 | 24,133 | 0.23x |


## 2026-06-10 08:30 ET  (premarket)

HL perp move +3.40% at 17.5σ on $1,154,203 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-09) = $27,639,289 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 178.96 | — | +0.00% | $2,040,342 | 11,405 | 0.07x |
| 08:25 | 179.35 | +0.22% | +0.22% | $185,855 | 1,037 | 0.01x |
| **08:30** | **185.75** | **+3.57%** | **+3.79%** | **$27,786,557** | **150,553** | **1.01x** |
| 08:35 | 184.19 | -0.84% | +2.92% | $14,200,164 | 76,884 | 0.51x |
| 08:40 | 184.34 | +0.08% | +3.01% | $2,207,467 | 11,984 | 0.08x |


## 2026-06-24 16:00 ET  (postmarket)

HL perp move +4.24% at 13.2σ on $3,048,216 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $30,113,944 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 196.59 | — | +0.00% | $51,853,430 | 264,507 | 1.72x |
| 15:55 | 196.84 | +0.13% | +0.13% | $70,683,343 | 359,761 | 2.35x |
| **16:00** | **200.91** | **+2.07%** | **+2.20%** | **$3,565,280** | **17,828** | **0.12x** |
| 16:05 | 205.50 | +2.28% | +4.53% | $24,241,389 | 118,853 | 0.80x |
| 16:10 | 205.20 | -0.15% | +4.38% | $16,006,468 | 77,868 | 0.53x |

