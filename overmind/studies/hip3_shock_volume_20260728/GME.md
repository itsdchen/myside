# GME — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:GME`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 1 earnings-linked, 2 other.**

---

# Earnings

## 2026-06-02 16:45 ET  (postmarket)

**EARNINGS 2026-06-02**  ·  HL perp move -2.81% at 6.0σ on $258,189 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $1,040,379 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:35 | 20.93 | — | +0.00% | $11,040 | 528 | 0.01x |
| 16:40 | 23.23 | +10.99% | +10.99% | $4,747,013 | 203,851 | 4.56x |
| **16:45** | **22.32** | **-3.91%** | **+6.64%** | **$2,692,364** | **118,622** | **2.59x** |
| 16:50 | 22.35 | +0.13% | +6.79% | $1,999,003 | 88,791 | 1.92x |
| 16:55 | 22.48 | +0.58% | +7.41% | $1,139,517 | 50,737 | 1.10x |


---

# Other (non-earnings)

## 2026-05-31 20:00 ET  (overnight)

HL perp move -4.70% at 10.2σ on $415,999 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $777,741 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **21.50** | **—** | **—** | **$328,127** | **15,202** | **0.42x** |
| 20:05 | 21.65 | +0.70% | — | $44,390 | 2,058 | 0.06x |
| 20:10 | 21.56 | -0.42% | — | $106,752 | 4,964 | 0.14x |


## 2026-06-26 17:30 ET  (postmarket)

HL perp move -3.03% at 6.5σ on $77,334 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-25) = $533,193 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:20 | 21.78 | — | +0.00% | $18,295 | 840 | 0.03x |
| 17:25 | 22.70 | +4.22% | +4.22% | $1,048,085 | 46,676 | 1.97x |
| **17:30** | **22.23** | **-2.07%** | **+2.07%** | **$712,617** | **31,683** | **1.34x** |
| 17:35 | 22.11 | -0.54% | +1.52% | $355,401 | 15,982 | 0.67x |
| 17:40 | 22.28 | +0.77% | +2.30% | $342,366 | 15,432 | 0.64x |

