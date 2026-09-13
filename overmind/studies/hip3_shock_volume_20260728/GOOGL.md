# GOOGL — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:GOOGL`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**2 shocks: 2 earnings-linked, 0 other.**

---

# Earnings

## 2026-02-04 16:00 ET  (postmarket)

**EARNINGS 2026-02-04**  ·  HL perp move -0.88% at 83.3σ on $13,076,045 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-03) = $79,952,726 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 332.85 | — | +0.00% | $225,096,067 | 675,766 | 2.82x |
| 15:55 | 333.26 | +0.12% | +0.12% | $300,962,263 | 903,612 | 3.76x |
| **16:00** | **333.04** | **-0.07%** | **+0.06%** | **$582,624,662** | **1,810,180** | **7.29x** |
| 16:05 | 335.41 | +0.71% | +0.77% | $406,530,163 | 1,232,953 | 5.08x |
| 16:10 | 330.20 | -1.55% | -0.79% | $194,349,566 | 585,658 | 2.43x |


## 2026-07-22 16:00 ET  (postmarket)

**EARNINGS 2026-07-22**  ·  HL perp move +0.79% at 31.7σ on $31,075,057 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $49,606,566 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 343.64 | — | +0.00% | $212,903,550 | 618,984 | 4.29x |
| 15:55 | 342.15 | -0.43% | -0.43% | $441,506,133 | 1,289,947 | 8.90x |
| **16:00** | **337.25** | **-1.43%** | **-1.86%** | **$160,864,019** | **468,678** | **3.24x** |
| 16:05 | 339.30 | +0.61% | -1.26% | $245,372,537 | 725,320 | 4.95x |
| 16:10 | 350.09 | +3.18% | +1.88% | $132,261,262 | 383,662 | 2.67x |

