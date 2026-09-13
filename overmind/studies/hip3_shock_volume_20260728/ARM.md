# ARM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:ARM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-06-24 16:05 ET  (postmarket)

HL perp move +2.09% at 12.3σ on $427,730 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $27,181,928 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 357.46 | — | +0.00% | $71,974,105 | 201,395 | 2.65x |
| 16:00 | 362.47 | +1.40% | +1.40% | $2,658,020 | 7,337 | 0.10x |
| **16:05** | **375.41** | **+3.57%** | **+5.02%** | **$16,729,594** | **45,336** | **0.62x** |
| 16:10 | 368.53 | -1.83% | +3.10% | $8,934,054 | 24,112 | 0.33x |
| 16:15 | 368.94 | +0.11% | +3.21% | $3,297,670 | 8,934 | 0.12x |

