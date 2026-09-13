# CBRS — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:CBRS`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**4 shocks: 1 earnings-linked, 3 other.**

---

# Earnings

## 2026-06-23 16:55 ET  (postmarket)

**EARNINGS 2026-06-23**  ·  HL perp move -3.69% at 9.6σ on $1,492,596 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-22) = $8,554,124 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:45 | 219.50 | — | +0.00% | $14,448,820 | 65,455 | 1.69x |
| 16:50 | 218.82 | -0.31% | -0.31% | $17,950,912 | 83,507 | 2.10x |
| **16:55** | **210.09** | **-3.99%** | **-4.29%** | **$13,215,102** | **62,319** | **1.54x** |
| 17:00 | 211.48 | +0.66% | -3.65% | $14,598,542 | 69,832 | 1.71x |
| 17:05 | 209.99 | -0.70% | -4.33% | $9,389,949 | 44,670 | 1.10x |


---

# Other (non-earnings)

## 2026-05-15 05:45 ET  (premarket)

HL perp move -2.80% at 6.3σ on $5,438,116 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-14) = $114,706,952 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 05:35 | 310.17 | — | +0.00% | $1,262,500 | 4,057 | 0.01x |
| 05:40 | 307.77 | -0.77% | -0.77% | $909,259 | 2,941 | 0.01x |
| **05:45** | **299.00** | **-2.85%** | **-3.60%** | **$12,730,736** | **42,962** | **0.11x** |
| 05:50 | 300.00 | +0.33% | -3.28% | $4,073,433 | 13,729 | 0.04x |
| 05:55 | 302.03 | +0.68% | -2.62% | $2,651,491 | 8,778 | 0.02x |


## 2026-07-01 09:25 ET  (premarket)

HL perp move +3.38% at 7.4σ on $605,990 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-30) = $12,475,648 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 209.79 | — | +0.00% | $7,074,598 | 33,936 | 0.57x |
| 09:20 | 209.89 | +0.05% | +0.05% | $798,481 | 3,806 | 0.06x |
| **09:25** | **216.00** | **+2.91%** | **+2.96%** | **$6,895,325** | **32,183** | **0.55x** |
| 09:30 | 211.82 | -1.94% | +0.97% | $8,801,729 | 41,265 | 0.71x |
| 09:35 | 218.14 | +2.98% | +3.98% | $21,274,851 | 98,950 | 1.71x |


## 2026-07-08 04:35 ET  (premarket)

HL perp move -2.27% at 5.1σ on $348,354 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-07) = $6,276,431 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 04:25 | 172.34 | — | +0.00% | $352,258 | 2,042 | 0.06x |
| 04:30 | 172.42 | +0.05% | +0.05% | $156,337 | 906 | 0.02x |
| **04:35** | **170.27** | **-1.25%** | **-1.20%** | **$129,115** | **755** | **0.02x** |
| 04:40 | 171.03 | +0.45% | -0.76% | $287,740 | 1,688 | 0.05x |
| 04:45 | 172.84 | +1.06% | +0.29% | $58,149 | 338 | 0.01x |

