# AAPL — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:AAPL`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 1 earnings-linked, 0 other.**

---

# Earnings

## 2026-01-29 17:20 ET  (postmarket)

**EARNINGS 2026-01-29**  ·  HL perp move +0.44% at 30.5σ on $1,843,669 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-28) = $65,106,969 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:10 | 259.76 | — | +0.00% | $27,297,928 | 104,702 | 0.42x |
| 17:15 | 259.24 | -0.20% | -0.20% | $37,884,429 | 145,993 | 0.58x |
| **17:20** | **265.83** | **+2.54%** | **+2.34%** | **$224,849,415** | **851,931** | **3.45x** |
| 17:25 | 260.21 | -2.11% | +0.17% | $186,268,055 | 714,840 | 2.86x |
| 17:30 | 260.00 | -0.08% | +0.09% | $53,099,032 | 204,092 | 0.82x |

