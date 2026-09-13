# AMZN — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:AMZN`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**2 shocks: 2 earnings-linked, 0 other.**

---

# Earnings

## 2026-02-05 16:00 ET  (postmarket)

**EARNINGS 2026-02-05**  ·  HL perp move -8.82% at 74.3σ on $7,248,004 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-04) = $83,924,701 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 222.41 | — | +0.00% | $268,886,993 | 1,210,847 | 3.20x |
| 15:55 | 222.82 | +0.18% | +0.18% | $333,072,785 | 1,494,444 | 3.97x |
| **16:00** | **199.99** | **-10.25%** | **-10.08%** | **$499,917,032** | **2,454,828** | **5.96x** |
| 16:05 | 203.10 | +1.56% | -8.68% | $521,063,721 | 2,587,532 | 6.21x |
| 16:10 | 207.18 | +2.01% | -6.85% | $345,973,175 | 1,673,080 | 4.12x |


## 2026-04-29 16:00 ET  (postmarket)

**EARNINGS 2026-04-29**  ·  HL perp move -0.89% at 23.6σ on $1,384,184 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-04-28) = $74,720,125 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 262.50 | — | +0.00% | $269,519,993 | 1,027,041 | 3.61x |
| 15:55 | 262.55 | +0.02% | +0.02% | $259,717,766 | 989,071 | 3.48x |
| **16:00** | **259.00** | **-1.35%** | **-1.33%** | **$209,417,372** | **806,381** | **2.80x** |
| 16:05 | 263.00 | +1.54% | +0.19% | $251,414,390 | 956,917 | 3.36x |
| 16:10 | 254.25 | -3.33% | -3.14% | $152,339,151 | 588,470 | 2.04x |

