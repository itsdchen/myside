# BB — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:BB`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**6 shocks: 1 earnings-linked, 5 other.**

---

# Earnings

## 2026-06-25 07:00 ET  (premarket)

**EARNINGS 2026-06-25**  ·  HL perp move +2.74% at 5.3σ on $598,451 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-24) = $2,226,933 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:50 | 9.19 | — | +0.00% | $56,674 | 6,168 | 0.03x |
| 06:55 | 9.09 | -1.09% | -1.09% | $91,523 | 10,036 | 0.04x |
| **07:00** | **9.34** | **+2.75%** | **+1.63%** | **$1,644,056** | **177,188** | **0.74x** |
| 07:05 | 9.32 | -0.21% | +1.41% | $941,180 | 99,700 | 0.42x |
| 07:10 | 9.19 | -1.39% | +0.00% | $409,350 | 44,353 | 0.18x |


---

# Other (non-earnings)

## 2026-05-28 20:20 ET  (overnight)

HL perp move +3.58% at 5.1σ on $63,914 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-27) = $2,852,330 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:10 | 9.05 | — | +0.00% | $80,790 | 8,843 | 0.03x |
| 20:15 | 8.95 | -1.10% | -1.10% | $20,511 | 2,283 | 0.01x |
| **20:20** | **9.23** | **+3.13%** | **+1.99%** | **$54,569** | **5,922** | **0.02x** |
| 20:25 | 9.16 | -0.76% | +1.22% | $11,251 | 1,229 | 0.00x |
| 20:30 | 9.13 | -0.33% | +0.88% | $22,826 | 2,481 | 0.01x |


## 2026-06-02 17:15 ET  (postmarket)

HL perp move +7.28% at 7.0σ on $3,139,386 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $2,445,626 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:05 | 10.79 | — | +0.00% | $104,013 | 9,655 | 0.04x |
| 17:10 | 10.93 | +1.31% | +1.31% | $1,088,253 | 100,593 | 0.44x |
| **17:15** | **11.33** | **+3.64%** | **+5.00%** | **$1,642,680** | **147,524** | **0.67x** |
| 17:20 | 11.55 | +1.94% | +7.04% | $4,178,045 | 361,481 | 1.71x |
| 17:25 | 11.34 | -1.81% | +5.10% | $1,646,328 | 144,384 | 0.67x |


## 2026-06-03 16:30 ET  (postmarket)

HL perp move -4.02% at 7.2σ on $2,235,851 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-02) = $2,968,517 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:20 | 10.00 | — | +0.00% | $166,963 | 16,669 | 0.06x |
| 16:25 | 9.75 | -2.50% | -2.50% | $428,658 | 43,565 | 0.14x |
| **16:30** | **9.43** | **-3.28%** | **-5.70%** | **$1,017,252** | **106,390** | **0.34x** |
| 16:35 | 9.38 | -0.53% | -6.20% | $1,675,270 | 178,189 | 0.56x |
| 16:40 | 9.42 | +0.43% | -5.80% | $1,290,927 | 137,657 | 0.43x |


## 2026-06-24 21:05 ET  (overnight)

HL perp move -2.79% at 5.7σ on $2,457,618 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $1,101,003 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:55 | 9.32 | — | +0.00% | $561,600 | 60,252 | 0.51x |
| 21:00 | 9.07 | -2.68% | -2.68% | $513,694 | 56,287 | 0.47x |
| **21:05** | **8.81** | **-2.87%** | **-5.47%** | **$848,736** | **94,820** | **0.77x** |
| 21:10 | 8.56 | -2.84% | -8.15% | $304,216 | 35,094 | 0.28x |
| 21:15 | 8.85 | +3.39% | -5.04% | $317,792 | 36,177 | 0.29x |


## 2026-06-29 08:35 ET  (premarket)

HL perp move -2.68% at 5.4σ on $699,674 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-26) = $3,816,074 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:25 | 11.38 | — | +0.00% | $98,735 | 8,672 | 0.03x |
| 08:30 | 11.35 | -0.26% | -0.26% | $179,041 | 15,780 | 0.05x |
| **08:35** | **11.08** | **-2.34%** | **-2.59%** | **$760,376** | **67,965** | **0.20x** |
| 08:40 | 11.22 | +1.22% | -1.41% | $1,209,261 | 109,191 | 0.32x |
| 08:45 | 11.25 | +0.27% | -1.14% | $328,498 | 29,198 | 0.09x |

