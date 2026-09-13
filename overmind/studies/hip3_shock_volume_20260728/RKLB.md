# RKLB — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:RKLB`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 0 earnings-linked, 3 other.**

---

# Other (non-earnings)

## 2026-05-20 17:30 ET  (postmarket)

HL perp move -3.48% at 9.6σ on $359,752 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-19) = $31,014,990 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:20 | 130.09 | — | +0.00% | $4,973,850 | 37,910 | 0.16x |
| 17:25 | 130.01 | -0.06% | -0.06% | $10,747,056 | 82,879 | 0.35x |
| **17:30** | **125.88** | **-3.18%** | **-3.24%** | **$16,822,022** | **132,122** | **0.54x** |
| 17:35 | 127.13 | +1.00% | -2.28% | $12,115,464 | 95,909 | 0.39x |
| 17:40 | 126.70 | -0.34% | -2.61% | $5,931,811 | 46,679 | 0.19x |


## 2026-06-10 08:30 ET  (premarket)

HL perp move +4.61% at 8.0σ on $222,208 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-09) = $20,928,572 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 104.00 | — | +0.00% | $844,893 | 8,126 | 0.04x |
| 08:25 | 104.93 | +0.90% | +0.90% | $760,670 | 7,276 | 0.04x |
| **08:30** | **109.64** | **+4.49%** | **+5.42%** | **$6,676,545** | **61,457** | **0.32x** |
| 08:35 | 107.65 | -1.82% | +3.51% | $3,850,775 | 35,413 | 0.18x |
| 08:40 | 108.07 | +0.39% | +3.92% | $1,066,083 | 9,852 | 0.05x |


## 2026-07-21 17:10 ET  (postmarket)

HL perp move +2.23% at 6.0σ on $258,954 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-20) = $8,957,492 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:00 | 71.19 | — | +0.00% | $5,055,859 | 71,316 | 0.56x |
| 17:05 | 71.49 | +0.42% | +0.42% | $9,716,397 | 135,334 | 1.08x |
| **17:10** | **73.09** | **+2.24%** | **+2.67%** | **$9,103,601** | **125,119** | **1.02x** |
| 17:15 | 73.60 | +0.70% | +3.39% | $5,203,155 | 70,865 | 0.58x |
| 17:20 | 72.84 | -1.03% | +2.32% | $4,091,464 | 55,988 | 0.46x |

