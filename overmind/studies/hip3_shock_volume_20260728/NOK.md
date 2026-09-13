# NOK — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:NOK`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 1 earnings-linked, 0 other.**

---

# Earnings

## 2026-07-23 05:35 ET  (premarket)

**EARNINGS 2026-07-23**  ·  HL perp move -2.79% at 5.5σ on $146,117 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-22) = $5,295,777 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 05:25 | 10.63 | — | +0.00% | $527,517 | 49,543 | 0.10x |
| 05:30 | 10.49 | -1.32% | -1.32% | $2,107,308 | 200,138 | 0.40x |
| **05:35** | **10.21** | **-2.68%** | **-3.96%** | **$4,084,975** | **396,215** | **0.77x** |
| 05:40 | 10.39 | +1.77% | -2.26% | $3,695,555 | 358,514 | 0.70x |
| 05:45 | 10.36 | -0.29% | -2.54% | $3,009,973 | 289,020 | 0.57x |

