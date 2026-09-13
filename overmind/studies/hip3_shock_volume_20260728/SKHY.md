# SKHY — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:SKHY`), profiled against Nasdaq Basic (`XNAS.BASIC`) 1-minute bars.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- `vs prev` = same clock bucket on the previous trading day.
- The **bold** row is the shock bucket.

**3 shocks: 0 earnings-linked, 3 other.**

---

# Other (non-earnings)

## 2026-07-22 16:10 ET  (postmarket)

HL perp move +1.44% at 6.3σ on $1,666,024 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $47,431,694 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | Prev day | vs prev | vs base |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 16:00 | 166.67 | — | +0.00% | $4,118,584 | 24,751 | $3,003,438 | 1.4x | 0.09x |
| 16:05 | 166.18 | -0.30% | -0.30% | $3,201,876 | 19,364 | $3,766,746 | 0.9x | 0.07x |
| **16:10** | **168.50** | **+1.40%** | **+1.10%** | **$11,103,272** | **66,475** | **$1,388,770** | **8.0x** | **0.23x** |
| 16:15 | 169.00 | +0.30% | +1.40% | $14,216,701 | 84,193 | $1,363,843 | 10.4x | 0.30x |
| 16:20 | 169.80 | +0.47% | +1.88% | $6,363,730 | 37,542 | $2,915,292 | 2.2x | 0.13x |


## 2026-07-23 16:00 ET  (postmarket)

HL perp move +1.33% at 5.8σ on $2,024,453 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-22) = $28,242,619 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | Prev day | vs prev | vs base |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 15:50 | 169.91 | — | +0.00% | $43,840,396 | 258,374 | $43,025,896 | 1.0x | 1.55x |
| 15:55 | 169.42 | -0.29% | -0.29% | $102,815,516 | 606,679 | $80,510,891 | 1.3x | 3.64x |
| **16:00** | **171.64** | **+1.31%** | **+1.02%** | **$4,031,389** | **23,525** | **$4,118,584** | **1.0x** | **0.14x** |
| 16:05 | 171.10 | -0.31% | +0.70% | $8,336,688 | 48,688 | $3,201,876 | 2.6x | 0.30x |
| 16:10 | 171.53 | +0.25% | +0.95% | $5,181,149 | 30,185 | $11,103,272 | 0.5x | 0.18x |


## 2026-07-23 20:00 ET  (overnight)

HL perp move -1.74% at 5.4σ on $1,544,069 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-07-22) = $28,242,619 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | Prev day | vs prev | vs base |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | $0 | — | 0.00x |
| 19:55 | — | — | — | $0 | 0 | $0 | — | 0.00x |
| **20:00** | **167.00** | **—** | **—** | **$7,874,865** | **46,823** | **$0** | **—** | **0.28x** |
| 20:05 | 166.96 | -0.02% | — | $5,843,458 | 35,097 | $0 | — | 0.21x |
| 20:10 | 167.20 | +0.14% | — | $2,700,570 | 16,187 | $0 | — | 0.10x |

