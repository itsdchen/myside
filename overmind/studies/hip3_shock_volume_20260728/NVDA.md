# NVDA — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:NVDA`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 2 earnings-linked, 1 other.**

---

# Earnings

## 2025-11-19 16:20 ET  (postmarket)

**EARNINGS 2025-11-19**  ·  HL perp move +4.49% at 28.9σ on $11,892,354 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2025-11-18) = $315,699,424 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:10 | 187.29 | — | +0.00% | $53,866,718 | 287,820 | 0.17x |
| 16:15 | 186.72 | -0.30% | -0.30% | $56,058,280 | 300,126 | 0.18x |
| **16:20** | **191.16** | **+2.38%** | **+2.07%** | **$797,338,007** | **4,130,467** | **2.53x** |
| 16:25 | 191.75 | +0.31% | +2.38% | $291,419,137 | 1,522,002 | 0.92x |
| 16:30 | 193.58 | +0.96% | +3.36% | $299,882,934 | 1,552,856 | 0.95x |


## 2026-02-25 16:30 ET  (postmarket)

**EARNINGS 2026-02-25**  ·  HL perp move +2.90% at 34.2σ on $8,558,574 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-24) = $225,433,769 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:20 | 196.13 | — | +0.00% | $69,619,083 | 354,170 | 0.31x |
| 16:25 | 196.40 | +0.14% | +0.14% | $38,880,048 | 198,017 | 0.17x |
| **16:30** | **202.08** | **+2.89%** | **+3.03%** | **$778,853,255** | **3,875,158** | **3.45x** |
| 16:35 | 203.04 | +0.48% | +3.52% | $575,402,418 | 2,842,025 | 2.55x |
| 16:40 | 201.55 | -0.73% | +2.76% | $299,771,878 | 1,483,590 | 1.33x |


---

# Other (non-earnings)

## 2026-03-23 07:05 ET  (premarket)

HL perp move +3.57% at 30.9σ on $2,682,596 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-20) = $228,137,876 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 171.33 | — | +0.00% | $2,104,652 | 12,300 | 0.01x |
| 07:00 | 171.21 | -0.07% | -0.07% | $2,757,156 | 16,100 | 0.01x |
| **07:05** | **177.39** | **+3.61%** | **+3.54%** | **$64,224,202** | **364,492** | **0.28x** |
| 07:10 | 177.61 | +0.12% | +3.67% | $37,568,699 | 211,861 | 0.16x |
| 07:15 | 176.89 | -0.41% | +3.25% | $12,202,450 | 68,935 | 0.05x |

