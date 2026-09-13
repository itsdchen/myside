# MSFT — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:MSFT`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 1 earnings-linked, 0 other.**

---

# Earnings

## 2026-01-28 16:00 ET  (postmarket)

**EARNINGS 2026-01-28**  ·  HL perp move -4.39% at 77.7σ on $2,278,894 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-27) = $91,904,001 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 481.47 | — | +0.00% | $354,994,927 | 738,111 | 3.86x |
| 15:55 | 482.99 | +0.31% | +0.31% | $388,843,256 | 805,603 | 4.23x |
| **16:00** | **448.49** | **-7.14%** | **-6.85%** | **$206,062,246** | **461,102** | **2.24x** |
| 16:05 | 455.72 | +1.61% | -5.35% | $371,559,269 | 822,007 | 4.04x |
| 16:10 | 461.27 | +1.22% | -4.20% | $445,230,638 | 969,039 | 4.84x |

