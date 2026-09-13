# WDC — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:WDC`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-06-24 16:05 ET  (postmarket)

HL perp move +4.23% at 7.8σ on $38,345 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $52,332,454 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 641.71 | — | +0.00% | $133,857,728 | 208,855 | 2.56x |
| 16:00 | 657.60 | +2.48% | +2.48% | $10,307,667 | 15,763 | 0.20x |
| **16:05** | **686.09** | **+4.33%** | **+6.92%** | **$26,629,046** | **39,567** | **0.51x** |
| 16:10 | 695.47 | +1.37% | +8.38% | $53,876,373 | 77,789 | 1.03x |
| 16:15 | 700.00 | +0.65% | +9.08% | $21,387,849 | 30,835 | 0.41x |

