# MRVL — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:MRVL`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**11 shocks: 1 earnings-linked, 10 other.**

---

# Earnings

## 2026-05-27 16:05 ET  (postmarket)

**EARNINGS 2026-05-27**  ·  HL perp move +3.77% at 18.5σ on $4,690,951 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-26) = $57,427,380 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 198.77 | — | +0.00% | $215,012,338 | 1,081,518 | 3.74x |
| 16:00 | 198.43 | -0.17% | -0.17% | $10,172,282 | 51,072 | 0.18x |
| **16:05** | **204.38** | **+3.00%** | **+2.82%** | **$116,883,767** | **571,725** | **2.04x** |
| 16:10 | 202.40 | -0.97% | +1.83% | $70,979,064 | 348,961 | 1.24x |
| 16:15 | 211.38 | +4.44% | +6.34% | $123,890,575 | 582,281 | 2.16x |


---

# Other (non-earnings)

## 2026-06-01 23:20 ET  (overnight)

HL perp move +13.24% at 13.8σ on $28,993,730 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-05-29) = $37,875,771 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 23:10 | 228.87 | — | +0.00% | $8,756,396 | 38,756 | 0.23x |
| 23:15 | 229.00 | +0.06% | +0.06% | $7,646,156 | 33,450 | 0.20x |
| **23:20** | **236.01** | **+3.06%** | **+3.12%** | **$8,262,939** | **35,371** | **0.22x** |
| 23:25 | 240.22 | +1.78% | +4.96% | $23,473,830 | 98,295 | 0.62x |
| 23:30 | 240.18 | -0.02% | +4.94% | $53,358,310 | 216,449 | 1.41x |


## 2026-06-02 00:00 ET  (overnight)

HL perp move -2.24% at 9.6σ on $20,367,120 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $53,918,960 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 23:50 | — | — | — | $0 | 0 | 0.00x |
| 23:55 | — | — | — | $0 | 0 | 0.00x |
| **00:00** | **259.00** | **—** | **—** | **$25,143,087** | **97,479** | **0.47x** |
| 00:05 | 261.90 | +1.12% | — | $28,175,897 | 107,485 | 0.52x |
| 00:10 | 255.56 | -2.42% | — | $21,161,877 | 82,603 | 0.39x |


## 2026-06-02 04:10 ET  (premarket)

HL perp move +3.08% at 10.6σ on $2,374,893 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $53,918,960 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 04:00 | 262.00 | — | +0.00% | $181,736,371 | 704,063 | 3.37x |
| 04:05 | 262.25 | +0.10% | +0.10% | $28,507,670 | 109,323 | 0.53x |
| **04:10** | **270.00** | **+2.96%** | **+3.05%** | **$42,546,894** | **159,649** | **0.79x** |
| 04:15 | 265.05 | -1.83% | +1.16% | $32,883,238 | 123,308 | 0.61x |
| 04:20 | 266.40 | +0.51% | +1.68% | $23,006,941 | 86,908 | 0.43x |


## 2026-06-02 04:25 ET  (premarket)

HL perp move +2.85% at 9.8σ on $965,209 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $53,918,960 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 04:15 | 265.05 | — | +0.00% | $32,883,238 | 123,308 | 0.61x |
| 04:20 | 266.40 | +0.51% | +0.51% | $23,006,941 | 86,908 | 0.43x |
| **04:25** | **274.46** | **+3.03%** | **+3.55%** | **$43,524,553** | **159,912** | **0.81x** |
| 04:30 | 270.99 | -1.26% | +2.24% | $42,609,477 | 155,426 | 0.79x |
| 04:35 | 270.33 | -0.24% | +1.99% | $16,907,808 | 62,182 | 0.31x |


## 2026-06-02 09:25 ET  (premarket)

HL perp move -2.22% at 7.8σ on $2,299,480 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-01) = $53,918,960 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 09:15 | 255.75 | — | +0.00% | $14,414,483 | 56,302 | 0.27x |
| 09:20 | 258.00 | +0.88% | +0.88% | $43,240,029 | 168,023 | 0.80x |
| **09:25** | **255.75** | **-0.87%** | **+0.00%** | **$49,898,051** | **194,237** | **0.93x** |
| 09:30 | 262.75 | +2.74% | +2.74% | $515,724,863 | 1,960,018 | 9.56x |
| 09:35 | 268.54 | +2.20% | +5.00% | $698,913,303 | 2,613,151 | 12.96x |


## 2026-06-03 20:30 ET  (overnight)

HL perp move +2.26% at 7.2σ on $1,838,418 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-02) = $189,709,313 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 20:20 | 288.00 | — | +0.00% | $4,052,762 | 14,089 | 0.02x |
| 20:25 | 290.47 | +0.86% | +0.86% | $9,904,352 | 34,093 | 0.05x |
| **20:30** | **297.16** | **+2.30%** | **+3.18%** | **$13,831,903** | **46,916** | **0.07x** |
| 20:35 | 295.00 | -0.73% | +2.43% | $18,503,437 | 62,562 | 0.10x |
| 20:40 | 294.00 | -0.34% | +2.08% | $9,120,057 | 31,005 | 0.05x |


## 2026-06-05 17:15 ET  (postmarket)

HL perp move +7.48% at 22.1σ on $13,228,647 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-04) = $197,138,951 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:05 | 259.55 | — | +0.00% | $10,179,671 | 39,272 | 0.05x |
| 17:10 | 262.43 | +1.11% | +1.11% | $15,217,478 | 58,090 | 0.08x |
| **17:15** | **277.58** | **+5.77%** | **+6.95%** | **$121,791,869** | **443,296** | **0.62x** |
| 17:20 | 282.02 | +1.60% | +8.66% | $87,153,444 | 310,320 | 0.44x |
| 17:25 | 280.63 | -0.49% | +8.12% | $58,867,720 | 209,954 | 0.30x |


## 2026-06-10 08:30 ET  (premarket)

HL perp move +3.67% at 12.6σ on $2,310,185 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-09) = $205,463,087 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 255.95 | — | +0.00% | $4,011,925 | 15,742 | 0.02x |
| 08:25 | 257.25 | +0.51% | +0.51% | $8,581,612 | 33,381 | 0.04x |
| **08:30** | **267.00** | **+3.79%** | **+4.32%** | **$67,978,017** | **255,090** | **0.33x** |
| 08:35 | 264.30 | -1.01% | +3.26% | $32,849,274 | 123,681 | 0.16x |
| 08:40 | 263.77 | -0.20% | +3.06% | $22,701,578 | 86,115 | 0.11x |


## 2026-06-24 16:05 ET  (postmarket)

HL perp move +2.41% at 9.2σ on $857,883 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-23) = $80,830,259 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:55 | 275.17 | — | +0.00% | $198,647,830 | 721,905 | 2.46x |
| 16:00 | 279.96 | +1.74% | +1.74% | $291,816,104 | 1,043,969 | 3.61x |
| **16:05** | **286.25** | **+2.25%** | **+4.03%** | **$46,037,999** | **161,841** | **0.57x** |
| 16:10 | 284.90 | -0.47% | +3.54% | $25,330,782 | 88,925 | 0.31x |
| 16:15 | 285.07 | +0.06% | +3.60% | $13,208,794 | 46,333 | 0.16x |


## 2026-07-14 08:30 ET  (premarket)

HL perp move +2.18% at 7.5σ on $823,882 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-13) = $37,435,380 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:20 | 225.80 | — | +0.00% | $1,067,547 | 4,740 | 0.03x |
| 08:25 | 226.10 | +0.13% | +0.13% | $2,893,355 | 12,804 | 0.08x |
| **08:30** | **230.93** | **+2.13%** | **+2.27%** | **$14,592,520** | **63,393** | **0.39x** |
| 08:35 | 229.68 | -0.54% | +1.72% | $10,944,550 | 47,691 | 0.29x |
| 08:40 | 229.89 | +0.09% | +1.81% | $5,723,337 | 24,873 | 0.15x |

