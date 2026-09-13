# META — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:META`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**2 shocks: 1 earnings-linked, 1 other.**

---

# Earnings

## 2026-01-28 16:00 ET  (postmarket)

**EARNINGS 2026-01-28**  ·  HL perp move +3.43% at 66.2σ on $4,240,522 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-27) = $63,251,118 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 669.19 | — | +0.00% | $224,895,826 | 335,081 | 3.56x |
| 15:55 | 670.03 | +0.13% | +0.13% | $318,689,295 | 475,603 | 5.04x |
| **16:00** | **652.65** | **-2.59%** | **-2.47%** | **$215,763,100** | **331,934** | **3.41x** |
| 16:05 | 691.27 | +5.92% | +3.30% | $435,494,807 | 642,231 | 6.89x |
| 16:10 | 692.23 | +0.14% | +3.44% | $351,354,231 | 504,137 | 5.55x |


---

# Other (non-earnings)

## 2025-12-04 09:05 ET  (premarket)

HL perp move +2.27% at 17.6σ on $768,383 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2025-12-03) = $44,843,037 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:55 | 643.60 | — | +0.00% | $1,067,845 | 1,659 | 0.02x |
| 09:00 | 669.61 | +4.04% | +4.04% | $289,652,173 | 432,623 | 6.46x |
| **09:05** | **680.00** | **+1.55%** | **+5.66%** | **$280,913,996** | **414,880** | **6.26x** |
| 09:10 | 682.84 | +0.42% | +6.10% | $346,525,772 | 507,137 | 7.73x |
| 09:15 | 678.29 | -0.67% | +5.39% | $255,461,535 | 375,464 | 5.70x |

