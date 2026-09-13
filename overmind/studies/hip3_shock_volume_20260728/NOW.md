# NOW — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:NOW`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 1 earnings-linked, 0 other.**

---

# Earnings

## 2026-07-22 16:15 ET  (postmarket)

**EARNINGS 2026-07-22**  ·  HL perp move +4.88% at 10.6σ on $535,993 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $11,933,203 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:05 | 96.52 | — | +0.00% | $11,259,336 | 117,630 | 0.94x |
| 16:10 | 97.60 | +1.12% | +1.12% | $74,568,232 | 759,677 | 6.25x |
| **16:15** | **102.65** | **+5.17%** | **+6.35%** | **$71,872,567** | **712,790** | **6.02x** |
| 16:20 | 101.15 | -1.46% | +4.80% | $38,492,619 | 379,776 | 3.23x |
| 16:25 | 99.89 | -1.25% | +3.49% | $24,927,919 | 249,083 | 2.09x |

