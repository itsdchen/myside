# BOT — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:BOT`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-07-02 04:25 ET  (premarket)

HL perp move -7.33% at 6.2σ on $818,374 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-01) = $467,557 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 04:15 | 38.90 | — | +0.00% | $78 | 2 | 0.00x |
| 04:20 | 38.40 | -1.29% | -1.29% | $9,069 | 235 | 0.02x |
| **04:25** | **37.00** | **-3.65%** | **-4.88%** | **$62,917** | **1,689** | **0.13x** |
| 04:30 | 34.12 | -7.78% | -12.29% | $149,867 | 4,362 | 0.32x |
| 04:35 | 35.24 | +3.28% | -9.41% | $8,601 | 246 | 0.02x |

