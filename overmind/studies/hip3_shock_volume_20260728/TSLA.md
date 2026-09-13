# TSLA — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:TSLA`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**4 shocks: 3 earnings-linked, 1 other.**

---

# Earnings

## 2026-01-28 16:05 ET  (postmarket)

**EARNINGS 2026-01-28**  ·  HL perp move +4.07% at 43.0σ on $1,725,633 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-27) = $148,798,329 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 432.17 | — | +0.00% | $314,398,274 | 726,426 | 2.11x |
| 16:00 | 429.04 | -0.73% | -0.73% | $115,666,607 | 269,290 | 0.78x |
| **16:05** | **446.60** | **+4.09%** | **+3.34%** | **$428,506,698** | **965,458** | **2.88x** |
| 16:10 | 443.99 | -0.58% | +2.74% | $171,370,433 | 386,002 | 1.15x |
| 16:15 | 446.46 | +0.56% | +3.31% | $207,265,488 | 465,644 | 1.39x |


## 2026-04-22 16:00 ET  (postmarket)

**EARNINGS 2026-04-22**  ·  HL perp move +4.11% at 43.4σ on $2,343,629 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-04-21) = $177,443,599 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 387.80 | — | +0.00% | $364,695,729 | 940,763 | 2.06x |
| 15:55 | 387.63 | -0.04% | -0.04% | $372,305,811 | 960,842 | 2.10x |
| **16:00** | **403.82** | **+4.18%** | **+4.13%** | **$391,212,103** | **979,032** | **2.20x** |
| 16:05 | 402.40 | -0.35% | +3.76% | $378,137,250 | 942,120 | 2.13x |
| 16:10 | 405.25 | +0.71% | +4.50% | $289,515,743 | 715,948 | 1.63x |


## 2026-07-22 16:00 ET  (postmarket)

**EARNINGS 2026-07-22**  ·  HL perp move -2.02% at 22.0σ on $3,032,225 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $89,313,166 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 373.13 | — | +0.00% | $162,544,817 | 434,720 | 1.82x |
| 15:55 | 374.04 | +0.24% | +0.24% | $167,064,729 | 446,856 | 1.87x |
| **16:00** | **366.40** | **-2.04%** | **-1.80%** | **$42,064,960** | **114,400** | **0.47x** |
| 16:05 | 365.27 | -0.31% | -2.11% | $162,055,721 | 446,522 | 1.81x |
| 16:10 | 364.32 | -0.26% | -2.36% | $84,644,860 | 232,549 | 0.95x |


---

# Other (non-earnings)

## 2026-03-23 07:05 ET  (premarket)

HL perp move +3.24% at 28.2σ on $1,457,614 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-20) = $225,276,316 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 358.38 | — | +0.00% | $2,636,043 | 7,359 | 0.01x |
| 07:00 | 358.93 | +0.15% | +0.15% | $7,429,174 | 20,691 | 0.03x |
| **07:05** | **370.84** | **+3.32%** | **+3.48%** | **$39,493,523** | **107,388** | **0.18x** |
| 07:10 | 372.24 | +0.38% | +3.87% | $42,273,444 | 113,770 | 0.19x |
| 07:15 | 372.68 | +0.12% | +3.99% | $33,532,707 | 89,988 | 0.15x |

