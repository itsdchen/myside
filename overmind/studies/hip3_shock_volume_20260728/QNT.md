# QNT — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:QNT`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**4 shocks: 0 earnings-linked, 4 other.**

---

# Other (non-earnings)

## 2026-06-05 00:35 ET  (overnight)

HL perp move +2.54% at 5.5σ on $130,649 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-04) = $17,857,130 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 00:25 | 59.69 | — | +0.00% | $5,372 | 90 | 0.00x |
| 00:30 | 59.00 | -1.16% | -1.16% | $283,004 | 4,793 | 0.02x |
| **00:35** | **59.69** | **+1.17%** | **+0.00%** | **$42,354** | **717** | **0.00x** |
| 00:40 | 58.84 | -1.42% | -1.42% | $39,324 | 667 | 0.00x |
| 00:45 | 58.91 | +0.12% | -1.31% | $116,573 | 2,015 | 0.01x |


## 2026-06-05 02:30 ET  (overnight)

HL perp move -3.00% at 9.7σ on $7,520,471 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-04) = $17,857,130 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 02:20 | 58.05 | — | +0.00% | $42,129 | 725 | 0.00x |
| 02:25 | 58.03 | -0.03% | -0.03% | $74,843 | 1,290 | 0.00x |
| **02:30** | **55.31** | **-4.69%** | **-4.72%** | **$237,600** | **4,187** | **0.01x** |
| 02:35 | 53.90 | -2.55% | -7.15% | $530,745 | 9,801 | 0.03x |
| 02:40 | 56.06 | +4.01% | -3.43% | $847,238 | 15,376 | 0.05x |


## 2026-06-05 03:05 ET  (overnight)

HL perp move +3.10% at 6.7σ on $128,719 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-04) = $17,857,130 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 02:55 | 56.35 | — | +0.00% | $21,795 | 387 | 0.00x |
| 03:00 | 55.62 | -1.30% | -1.30% | $110,182 | 1,955 | 0.01x |
| **03:05** | **56.51** | **+1.60%** | **+0.28%** | **$154,408** | **2,779** | **0.01x** |
| 03:10 | 57.35 | +1.49% | +1.77% | $65,073 | 1,143 | 0.00x |
| 03:15 | 57.78 | +0.75% | +2.54% | $161,897 | 2,820 | 0.01x |


## 2026-06-05 16:00 ET  (postmarket)

HL perp move -2.07% at 5.0σ on $67,560 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-04) = $17,857,130 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 54.88 | — | +0.00% | $7,794,302 | 142,619 | 0.44x |
| 15:55 | 56.50 | +2.94% | +2.94% | $14,845,027 | 264,708 | 0.83x |
| **16:00** | **55.50** | **-1.77%** | **+1.12%** | **$28,255** | **511** | **0.00x** |
| 16:05 | 55.75 | +0.45% | +1.58% | $417,933 | 7,516 | 0.02x |
| 16:10 | 55.15 | -1.08% | +0.48% | $100,591 | 1,820 | 0.01x |

