# LITE — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:LITE`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-05-25 20:00 ET  (overnight)

HL perp move -4.54% at 13.9σ on $674,051 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-22) = $20,893,833 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **971.02** | **—** | **—** | **$4,030,147** | **4,157** | **0.19x** |
| 20:05 | 960.00 | -1.13% | — | $1,425,487 | 1,476 | 0.07x |
| 20:10 | 967.99 | +0.83% | — | $1,493,940 | 1,550 | 0.07x |

