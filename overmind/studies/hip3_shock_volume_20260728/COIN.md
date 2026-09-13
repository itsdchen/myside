# COIN — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:COIN`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 2 earnings-linked, 1 other.**

---

# Earnings

## 2026-02-12 16:05 ET  (postmarket)

**EARNINGS 2026-02-12**  ·  HL perp move -0.01% at 21.1σ on $1,017,708 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-11) = $16,061,645 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 141.54 | — | +0.00% | $57,675,307 | 407,141 | 3.59x |
| 16:00 | 141.09 | -0.32% | -0.32% | $21,560,712 | 152,830 | 1.34x |
| **16:05** | **136.50** | **-3.25%** | **-3.56%** | **$19,542,930** | **141,569** | **1.22x** |
| 16:10 | 141.97 | +4.01% | +0.30% | $21,543,572 | 155,667 | 1.34x |
| 16:15 | 143.48 | +1.06% | +1.37% | $17,287,664 | 121,892 | 1.08x |


## 2026-05-07 16:10 ET  (postmarket)

**EARNINGS 2026-05-07**  ·  HL perp move -3.24% at 13.2σ on $4,126,836 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-06) = $10,496,879 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:00 | 194.00 | — | +0.00% | $1,165,552 | 6,008 | 0.11x |
| 16:05 | 190.90 | -1.60% | -1.60% | $4,151,166 | 21,345 | 0.40x |
| **16:10** | **190.00** | **-0.47%** | **-2.06%** | **$9,349,039** | **48,918** | **0.89x** |
| 16:15 | 186.25 | -1.97% | -3.99% | $14,911,455 | 80,540 | 1.42x |
| 16:20 | 184.75 | -0.81% | -4.77% | $9,186,996 | 49,765 | 0.88x |


---

# Other (non-earnings)

## 2025-11-30 20:00 ET  (overnight)

HL perp move -3.30% at 15.4σ on $608,849 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2025-11-28) = $23,265,223 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **261.66** | **—** | **—** | **$7,044,028** | **27,014** | **0.30x** |
| 20:05 | 263.00 | +0.51% | — | $2,377,431 | 9,081 | 0.10x |
| 20:10 | 261.50 | -0.57% | — | $2,398,858 | 9,189 | 0.10x |

