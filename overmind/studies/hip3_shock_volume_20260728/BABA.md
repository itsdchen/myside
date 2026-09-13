# BABA — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:BABA`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 1 earnings-linked, 2 other.**

---

# Earnings

## 2026-05-13 05:40 ET  (premarket)

**EARNINGS 2026-05-13**  ·  HL perp move -2.43% at 10.9σ on $75,408 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-12) = $13,478,184 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 05:30 | 137.10 | — | +0.00% | $19,599,769 | 144,380 | 1.45x |
| 05:35 | 136.84 | -0.19% | -0.19% | $10,395,262 | 75,537 | 0.77x |
| **05:40** | **134.00** | **-2.08%** | **-2.26%** | **$8,083,817** | **59,641** | **0.60x** |
| 05:45 | 133.90 | -0.07% | -2.33% | $6,824,615 | 51,151 | 0.51x |
| 05:50 | 132.01 | -1.41% | -3.71% | $6,372,018 | 47,992 | 0.47x |


---

# Other (non-earnings)

## 2026-03-25 01:35 ET  (overnight)

HL perp move +2.33% at 6.9σ on $334,270 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-24) = $6,078,036 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 01:25 | 128.49 | — | +0.00% | $2,768,790 | 21,617 | 0.46x |
| 01:30 | 128.61 | +0.09% | +0.09% | $2,501,865 | 19,412 | 0.41x |
| **01:35** | **132.00** | **+2.64%** | **+2.73%** | **$6,911,323** | **52,807** | **1.14x** |
| 01:40 | 132.91 | +0.69% | +3.44% | $13,812,054 | 103,715 | 2.27x |
| 01:45 | 131.42 | -1.12% | +2.28% | $6,448,337 | 48,909 | 1.06x |


## 2026-07-08 01:00 ET  (overnight)

HL perp move +2.22% at 6.6σ on $257,289 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-07-07) = $5,712,704 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 00:50 | 105.05 | — | +0.00% | $994,613 | 9,473 | 0.17x |
| 00:55 | 105.12 | +0.07% | +0.07% | $1,259,782 | 11,987 | 0.22x |
| **01:00** | **107.45** | **+2.22%** | **+2.28%** | **$7,845,703** | **73,628** | **1.37x** |
| 01:05 | 108.34 | +0.83% | +3.13% | $5,770,246 | 53,522 | 1.01x |
| 01:10 | 108.16 | -0.17% | +2.96% | $7,570,914 | 70,320 | 1.33x |

