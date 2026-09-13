# MSTR — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:MSTR`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 1 earnings-linked, 2 other.**

---

# Earnings

## 2026-02-05 17:05 ET  (postmarket)

**EARNINGS 2026-02-05**  ·  HL perp move +4.22% at 9.3σ on $422,070 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-04) = $25,994,877 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:55 | 102.52 | — | +0.00% | $4,765,573 | 46,239 | 0.18x |
| 17:00 | 102.04 | -0.47% | -0.47% | $7,384,751 | 72,470 | 0.28x |
| **17:05** | **103.75** | **+1.67%** | **+1.20%** | **$5,192,371** | **50,075** | **0.20x** |
| 17:10 | 105.99 | +2.16% | +3.38% | $3,586,554 | 34,168 | 0.14x |
| 17:15 | 103.41 | -2.43% | +0.87% | $2,630,600 | 25,274 | 0.10x |


---

# Other (non-earnings)

## 2026-06-29 08:00 ET  (premarket)

HL perp move +4.86% at 18.5σ on $4,538,954 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-26) = $25,465,828 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 07:50 | 83.75 | — | +0.00% | $267,145 | 3,188 | 0.01x |
| 07:55 | 84.00 | +0.30% | +0.30% | $363,547 | 4,340 | 0.01x |
| **08:00** | **88.08** | **+4.86%** | **+5.17%** | **$22,948,200** | **265,909** | **0.90x** |
| 08:05 | 87.81 | -0.31% | +4.85% | $16,562,500 | 187,070 | 0.65x |
| 08:10 | 88.80 | +1.13% | +6.03% | $8,969,747 | 101,841 | 0.35x |


## 2026-07-06 07:55 ET  (premarket)

HL perp move -4.02% at 10.8σ on $2,359,691 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-02) = $24,592,807 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 07:45 | 103.87 | — | +0.00% | $349,923 | 3,359 | 0.01x |
| 07:50 | 103.61 | -0.25% | -0.25% | $1,596,564 | 15,362 | 0.06x |
| **07:55** | **102.31** | **-1.25%** | **-1.50%** | **$4,418,174** | **42,921** | **0.18x** |
| 08:00 | 99.55 | -2.70% | -4.16% | $9,112,162 | 90,638 | 0.37x |
| 08:05 | 98.68 | -0.87% | -4.99% | $7,529,473 | 76,134 | 0.31x |

