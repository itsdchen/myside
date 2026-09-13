# ORCL — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:ORCL`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**6 shocks: 2 earnings-linked, 4 other.**

---

# Earnings

## 2026-03-10 16:05 ET  (postmarket)

**EARNINGS 2026-03-10**  ·  HL perp move +7.11% at 25.2σ on $2,013,277 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-09) = $30,684,577 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 149.66 | — | +0.00% | $111,866,028 | 746,513 | 3.65x |
| 16:00 | 150.36 | +0.47% | +0.47% | $119,220,085 | 792,884 | 3.89x |
| **16:05** | **158.24** | **+5.24%** | **+5.74%** | **$221,226,683** | **1,401,340** | **7.21x** |
| 16:10 | 160.00 | +1.11% | +6.91% | $115,978,605 | 725,888 | 3.78x |
| 16:15 | 160.83 | +0.52% | +7.47% | $73,662,950 | 460,587 | 2.40x |


## 2026-06-10 17:25 ET  (postmarket)

**EARNINGS 2026-06-10**  ·  HL perp move +2.35% at 10.7σ on $524,057 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-09) = $35,300,587 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:15 | 186.51 | — | +0.00% | $20,249,707 | 107,841 | 0.57x |
| 17:20 | 185.87 | -0.34% | -0.34% | $72,512,593 | 390,002 | 2.05x |
| **17:25** | **190.67** | **+2.58%** | **+2.23%** | **$44,597,562** | **234,969** | **1.26x** |
| 17:30 | 188.98 | -0.89% | +1.32% | $22,617,628 | 119,711 | 0.64x |
| 17:35 | 190.50 | +0.80% | +2.14% | $36,428,800 | 189,841 | 1.03x |


---

# Other (non-earnings)

## 2026-02-01 20:00 ET  (overnight)

HL perp move -3.47% at 16.9σ on $488,549 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-01-30) = $24,902,270 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **156.88** | **—** | **—** | **$4,885,564** | **30,684** | **0.20x** |
| 20:05 | 158.28 | +0.89% | — | $3,698,887 | 23,375 | 0.15x |
| 20:10 | 157.80 | -0.30% | — | $3,102,642 | 19,608 | 0.12x |


## 2026-05-31 20:05 ET  (overnight)

HL perp move +1.53% at 12.6σ on $1,045,823 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $52,549,517 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| 20:00 | 232.11 | — | — | $4,643,519 | 20,162 | 0.09x |
| **20:05** | **238.09** | **+2.58%** | **—** | **$6,387,847** | **26,993** | **0.12x** |
| 20:10 | 235.84 | -0.95% | — | $5,473,451 | 23,130 | 0.10x |
| 20:15 | 235.00 | -0.36% | — | $5,057,470 | 21,576 | 0.10x |


## 2026-06-03 17:30 ET  (postmarket)

HL perp move -2.28% at 10.7σ on $5,357,981 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-02) = $45,648,481 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:20 | 222.43 | — | +0.00% | $1,309,508 | 5,884 | 0.03x |
| 17:25 | 221.75 | -0.31% | -0.31% | $5,367,597 | 24,172 | 0.12x |
| **17:30** | **217.25** | **-2.03%** | **-2.33%** | **$9,359,900** | **42,885** | **0.21x** |
| 17:35 | 219.47 | +1.02% | -1.33% | $3,256,738 | 14,873 | 0.07x |
| 17:40 | 220.60 | +0.51% | -0.82% | $2,559,190 | 11,624 | 0.06x |


## 2026-06-23 08:50 ET  (premarket)

HL perp move -1.15% at 10.1σ on $2,228,086 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-22) = $22,848,414 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:40 | 170.50 | — | +0.00% | $762,382 | 4,477 | 0.03x |
| 08:45 | 170.35 | -0.09% | -0.09% | $705,871 | 4,145 | 0.03x |
| **08:50** | **166.00** | **-2.55%** | **-2.64%** | **$17,856,498** | **107,490** | **0.78x** |
| 08:55 | 168.58 | +1.55% | -1.13% | $12,249,462 | 72,790 | 0.54x |
| 09:00 | 168.69 | +0.07% | -1.06% | $4,391,588 | 26,064 | 0.19x |

