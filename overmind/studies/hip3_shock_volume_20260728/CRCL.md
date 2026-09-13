# CRCL — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:CRCL`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**8 shocks: 2 earnings-linked, 6 other.**

---

# Earnings

## 2026-02-25 06:30 ET  (premarket)

**EARNINGS 2026-02-25**  ·  HL perp move +6.17% at 9.8σ on $1,394,898 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-24) = $2,896,536 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:20 | 63.05 | — | +0.00% | $83,095 | 1,319 | 0.03x |
| 06:25 | 65.80 | +4.36% | +4.36% | $3,495,502 | 53,532 | 1.21x |
| **06:30** | **67.87** | **+3.15%** | **+7.64%** | **$4,033,450** | **60,013** | **1.39x** |
| 06:35 | 69.13 | +1.86% | +9.64% | $6,399,881 | 92,422 | 2.21x |
| 06:40 | 70.38 | +1.81% | +11.63% | $5,536,460 | 78,962 | 1.91x |


## 2026-05-11 06:25 ET  (premarket)

**EARNINGS 2026-05-11**  ·  HL perp move -6.00% at 15.7σ on $14,098,628 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-08) = $8,890,334 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:15 | 123.00 | — | +0.00% | $1,848,086 | 15,038 | 0.21x |
| 06:20 | 123.10 | +0.08% | +0.08% | $1,593,196 | 12,950 | 0.18x |
| **06:25** | **120.00** | **-2.52%** | **-2.44%** | **$14,425,455** | **121,133** | **1.62x** |
| 06:30 | 117.98 | -1.68% | -4.08% | $11,358,554 | 95,730 | 1.28x |
| 06:35 | 113.00 | -4.22% | -8.13% | $19,695,748 | 174,379 | 2.22x |


---

# Other (non-earnings)

## 2026-03-08 20:00 ET  (overnight)

HL perp move +0.87% at 9.8σ on $1,028,567 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-06) = $9,751,404 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **99.33** | **—** | **—** | **$3,370,305** | **33,825** | **0.35x** |
| 20:05 | 97.99 | -1.35% | — | $2,044,052 | 20,842 | 0.21x |
| 20:10 | 97.97 | -0.02% | — | $926,329 | 9,440 | 0.09x |


## 2026-03-30 20:45 ET  (overnight)

HL perp move +2.09% at 9.1σ on $204,071 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-03-27) = $7,957,024 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:35 | 89.24 | — | +0.00% | $146,460 | 1,641 | 0.02x |
| 20:40 | 89.65 | +0.46% | +0.46% | $125,707 | 1,405 | 0.02x |
| **20:45** | **91.61** | **+2.19%** | **+2.66%** | **$714,667** | **7,865** | **0.09x** |
| 20:50 | 91.43 | -0.20% | +2.45% | $568,270 | 6,217 | 0.07x |
| 20:55 | 91.09 | -0.37% | +2.07% | $272,140 | 2,988 | 0.03x |


## 2026-05-10 21:00 ET  (overnight)

HL perp move -2.34% at 10.4σ on $622,357 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-08) = $8,890,334 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:50 | 119.56 | — | +0.00% | $1,218,496 | 10,185 | 0.14x |
| 20:55 | 119.14 | -0.35% | -0.35% | $1,488,168 | 12,486 | 0.17x |
| **21:00** | **116.40** | **-2.30%** | **-2.64%** | **$4,782,472** | **40,772** | **0.54x** |
| 21:05 | 117.58 | +1.01% | -1.66% | $1,771,856 | 15,124 | 0.20x |
| 21:10 | 118.35 | +0.65% | -1.01% | $2,143,085 | 18,150 | 0.24x |


## 2026-06-30 09:20 ET  (premarket)

HL perp move -2.21% at 8.7σ on $2,243,904 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-29) = $3,583,496 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:10 | 73.94 | — | +0.00% | $508,437 | 6,860 | 0.14x |
| 09:15 | 73.79 | -0.20% | -0.20% | $252,519 | 3,424 | 0.07x |
| **09:20** | **72.00** | **-2.43%** | **-2.62%** | **$2,524,882** | **34,765** | **0.70x** |
| 09:25 | 72.35 | +0.49% | -2.15% | $2,356,550 | 32,568 | 0.66x |
| 09:30 | 72.97 | +0.85% | -1.32% | $6,560,023 | 89,866 | 1.83x |


## 2026-07-10 06:15 ET  (premarket)

HL perp move +4.08% at 8.8σ on $3,158,881 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-09) = $3,369,194 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:05 | 67.00 | — | +0.00% | $1,910,250 | 28,515 | 0.57x |
| 06:10 | 67.14 | +0.21% | +0.21% | $2,608,681 | 38,890 | 0.77x |
| **06:15** | **68.40** | **+1.88%** | **+2.09%** | **$4,477,667** | **65,731** | **1.33x** |
| 06:20 | 69.91 | +2.21% | +4.35% | $4,149,584 | 59,541 | 1.23x |
| 06:25 | 70.05 | +0.19% | +4.55% | $5,375,132 | 76,593 | 1.60x |


## 2026-07-10 07:05 ET  (premarket)

HL perp move +2.72% at 7.9σ on $2,106,981 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-09) = $3,369,194 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 70.36 | — | +0.00% | $1,826,154 | 26,008 | 0.54x |
| 07:00 | 70.38 | +0.03% | +0.03% | $4,763,567 | 67,826 | 1.41x |
| **07:05** | **71.87** | **+2.12%** | **+2.15%** | **$9,920,936** | **139,212** | **2.94x** |
| 07:10 | 73.19 | +1.84% | +4.02% | $10,261,759 | 140,670 | 3.05x |
| 07:15 | 72.34 | -1.16% | +2.81% | $10,172,024 | 139,699 | 3.02x |

