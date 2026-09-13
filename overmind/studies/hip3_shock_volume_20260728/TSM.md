# TSM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:TSM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-05-31 20:00 ET  (overnight)

HL perp move -2.15% at 10.2σ on $499,078 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $30,083,794 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **422.51** | **—** | **—** | **$626,643** | **1,485** | **0.02x** |
| 20:05 | 423.57 | +0.25% | — | $392,341 | 926 | 0.01x |
| 20:10 | 422.91 | -0.16% | — | $1,310,334 | 3,091 | 0.04x |

