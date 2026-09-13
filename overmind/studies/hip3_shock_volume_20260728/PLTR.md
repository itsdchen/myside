# PLTR — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:PLTR`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**5 shocks: 2 earnings-linked, 3 other.**

---

# Earnings

## 2026-02-02 16:05 ET  (postmarket)

**EARNINGS 2026-02-02**  ·  HL perp move +6.26% at 34.7σ on $6,503,843 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-30) = $51,924,757 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 148.11 | — | +0.00% | $178,183,549 | 1,204,185 | 3.43x |
| 16:00 | 148.87 | +0.51% | +0.51% | $52,177,647 | 351,550 | 1.00x |
| **16:05** | **158.79** | **+6.66%** | **+7.21%** | **$479,362,627** | **3,052,149** | **9.23x** |
| 16:10 | 157.41 | -0.87% | +6.28% | $110,432,225 | 698,271 | 2.13x |
| 16:15 | 158.51 | +0.70% | +7.02% | $130,466,604 | 822,181 | 2.51x |


## 2026-05-05 09:25 ET  (premarket)

**EARNINGS 2026-05-04**  ·  HL perp move -2.01% at 8.9σ on $215,673 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-04) = $66,057,837 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 143.15 | — | +0.00% | $17,253,424 | 120,306 | 0.26x |
| 09:20 | 143.03 | -0.08% | -0.08% | $14,291,590 | 99,959 | 0.22x |
| **09:25** | **140.73** | **-1.61%** | **-1.69%** | **$21,920,242** | **154,976** | **0.33x** |
| 09:30 | 141.35 | +0.44% | -1.26% | $239,877,106 | 1,706,091 | 3.63x |
| 09:35 | 141.17 | -0.13% | -1.38% | $264,435,584 | 1,863,872 | 4.00x |


---

# Other (non-earnings)

## 2025-11-30 20:00 ET  (overnight)

HL perp move -2.63% at 15.5σ on $601,864 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2025-11-28) = $28,973,604 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **165.00** | **—** | **—** | **$4,297,873** | **25,965** | **0.15x** |
| 20:05 | 165.50 | +0.30% | — | $949,998 | 5,741 | 0.03x |
| 20:10 | 165.17 | -0.20% | — | $1,174,853 | 7,113 | 0.04x |


## 2026-03-23 07:05 ET  (premarket)

HL perp move +3.03% at 13.1σ on $483,943 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-20) = $45,652,752 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 148.99 | — | +0.00% | $3,237,047 | 21,732 | 0.07x |
| 07:00 | 148.61 | -0.26% | -0.26% | $1,410,360 | 9,479 | 0.03x |
| **07:05** | **153.01** | **+2.96%** | **+2.70%** | **$6,649,570** | **43,954** | **0.15x** |
| 07:10 | 153.95 | +0.61% | +3.33% | $3,381,244 | 22,050 | 0.07x |
| 07:15 | 153.16 | -0.51% | +2.80% | $2,571,053 | 16,753 | 0.06x |


## 2026-05-31 20:00 ET  (overnight)

HL perp move -0.88% at 15.0σ on $5,019,635 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $94,422,310 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **160.09** | **—** | **—** | **$7,475,179** | **46,851** | **0.08x** |
| 20:05 | 160.59 | +0.31% | — | $6,555,545 | 40,779 | 0.07x |
| 20:10 | 161.20 | +0.38% | — | $6,108,235 | 37,929 | 0.06x |

