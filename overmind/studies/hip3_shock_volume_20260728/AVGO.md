# AVGO — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:AVGO`), profiled against Nasdaq Basic (`XNAS.BASIC`) 1-minute bars.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- `vs prev` = same clock bucket on the previous trading day.
- The **bold** row is the shock bucket.

**1 shocks: 1 earnings-linked, 0 other.**

---

# Earnings

## 2026-06-04 09:25 ET  (premarket)

**EARNINGS 2026-06-03**  ·  HL perp move -1.76% at 5.6σ on $608,426 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-03) = $115,768,168 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | Prev day | vs prev | vs base |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 09:15 | 411.62 | — | +0.00% | $637,934,766 | 1,345,706 | $5,253,436 | 121.4x | 5.51x |
| 09:20 | 416.50 | +1.19% | +1.19% | $82,018,969 | 197,819 | $8,005,627 | 10.2x | 0.71x |
| **09:25** | **409.65** | **-1.64%** | **-0.48%** | **$94,043,056** | **228,352** | **$11,786,600** | **8.0x** | **0.81x** |
| 09:30 | 410.35 | +0.17% | -0.31% | $451,141,875 | 1,101,008 | $274,786,386 | 1.6x | 3.90x |
| 09:35 | 408.25 | -0.51% | -0.82% | $620,675,290 | 1,515,540 | $379,994,243 | 1.6x | 5.36x |

