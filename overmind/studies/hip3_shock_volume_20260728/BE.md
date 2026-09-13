# BE — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:BE`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-07-01 09:25 ET  (premarket)

HL perp move -4.00% at 5.2σ on $159,119 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-30) = $22,084,555 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 302.99 | — | +0.00% | $14,141,715 | 46,952 | 0.64x |
| 09:20 | 297.41 | -1.84% | -1.84% | $15,476,455 | 52,052 | 0.70x |
| **09:25** | **293.41** | **-1.34%** | **-3.16%** | **$13,162,200** | **44,407** | **0.60x** |
| 09:30 | 303.37 | +3.39% | +0.12% | $134,561,772 | 442,789 | 6.09x |
| 09:35 | 306.32 | +0.97% | +1.10% | $111,831,696 | 365,428 | 5.06x |

