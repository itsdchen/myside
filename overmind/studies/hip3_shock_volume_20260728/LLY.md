# LLY — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:LLY`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**1 shocks: 0 earnings-linked, 1 other.**

---

# Other (non-earnings)

## 2026-06-07 22:15 ET  (overnight)

HL perp move +3.35% at 5.8σ on $583,042 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-05) = $29,614,389 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 22:05 | 1171.00 | — | +0.00% | $406,030 | 348 | 0.01x |
| 22:10 | 1185.05 | +1.20% | +1.20% | $804,054 | 680 | 0.03x |
| **22:15** | **1223.04** | **+3.21%** | **+4.44%** | **$3,090,250** | **2,555** | **0.10x** |
| 22:20 | 1200.50 | -1.84% | +2.52% | $1,221,430 | 1,014 | 0.04x |
| 22:25 | 1200.00 | -0.04% | +2.48% | $311,104 | 260 | 0.01x |

