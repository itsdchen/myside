# QCOM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:QCOM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 0 earnings-linked, 3 other.**

---

# Other (non-earnings)

## 2026-06-24 16:20 ET  (postmarket)

HL perp move +3.70% at 8.6σ on $63,742 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $31,477,742 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:10 | 199.49 | — | +0.00% | $5,488,134 | 27,331 | 0.17x |
| 16:15 | 200.99 | +0.75% | +0.75% | $6,124,957 | 30,508 | 0.19x |
| **16:20** | **208.92** | **+3.95%** | **+4.73%** | **$16,275,239** | **79,414** | **0.52x** |
| 16:25 | 212.45 | +1.69% | +6.50% | $26,581,492 | 124,727 | 0.84x |
| 16:30 | 216.11 | +1.72% | +8.33% | $17,774,184 | 82,553 | 0.56x |


## 2026-06-24 16:40 ET  (postmarket)

HL perp move +2.77% at 6.5σ on $118,698 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $31,477,742 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:30 | 216.11 | — | +0.00% | $17,774,184 | 82,553 | 0.56x |
| 16:35 | 219.06 | +1.37% | +1.37% | $13,807,315 | 63,788 | 0.44x |
| **16:40** | **225.35** | **+2.87%** | **+4.28%** | **$24,747,212** | **110,285** | **0.79x** |
| 16:45 | 228.45 | +1.38% | +5.71% | $17,191,053 | 75,733 | 0.55x |
| 16:50 | 224.56 | -1.70% | +3.91% | $16,037,996 | 71,027 | 0.51x |


## 2026-06-24 16:55 ET  (postmarket)

HL perp move -2.27% at 5.4σ on $146,593 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $31,477,742 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:45 | 228.45 | — | +0.00% | $17,191,053 | 75,733 | 0.55x |
| 16:50 | 224.56 | -1.70% | -1.70% | $16,037,996 | 71,027 | 0.51x |
| **16:55** | **220.00** | **-2.03%** | **-3.70%** | **$9,290,410** | **41,949** | **0.30x** |
| 17:00 | 221.92 | +0.87% | -2.86% | $7,496,092 | 34,001 | 0.24x |
| 17:05 | 222.56 | +0.29% | -2.58% | $4,085,310 | 18,397 | 0.13x |

