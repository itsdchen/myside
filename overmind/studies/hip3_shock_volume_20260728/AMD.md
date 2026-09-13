# AMD — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:AMD`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**5 shocks: 2 earnings-linked, 3 other.**

---

# Earnings

## 2026-02-03 16:15 ET  (postmarket)

**EARNINGS 2026-02-03**  ·  HL perp move -6.97% at 35.0σ on $1,837,143 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-02) = $73,356,043 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:05 | 242.85 | — | +0.00% | $17,305,585 | 71,044 | 0.24x |
| 16:10 | 243.56 | +0.29% | +0.29% | $19,303,862 | 79,590 | 0.26x |
| **16:15** | **229.27** | **-5.87%** | **-5.59%** | **$237,030,088** | **1,013,970** | **3.23x** |
| 16:20 | 232.84 | +1.56% | -4.12% | $172,825,357 | 747,248 | 2.36x |
| 16:25 | 230.40 | -1.05% | -5.13% | $130,093,096 | 560,320 | 1.77x |


## 2026-05-05 16:15 ET  (postmarket)

**EARNINGS 2026-05-05**  ·  HL perp move +5.26% at 16.5σ on $11,933,259 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-04) = $120,882,012 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:05 | 355.94 | — | +0.00% | $25,884,714 | 72,820 | 0.21x |
| 16:10 | 355.76 | -0.05% | -0.05% | $18,435,266 | 51,772 | 0.15x |
| **16:15** | **360.50** | **+1.33%** | **+1.28%** | **$441,646,987** | **1,254,319** | **3.65x** |
| 16:20 | 371.32 | +3.00% | +4.32% | $371,313,243 | 1,010,821 | 3.07x |
| 16:25 | 379.74 | +2.27% | +6.69% | $346,008,788 | 917,551 | 2.86x |


---

# Other (non-earnings)

## 2026-02-24 07:05 ET  (premarket)

HL perp move +2.15% at 8.8σ on $415,786 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-23) = $45,444,267 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:55 | 197.40 | — | +0.00% | $84,494 | 428 | 0.00x |
| 07:00 | 221.28 | +12.10% | +12.10% | $118,332,647 | 541,108 | 2.60x |
| **07:05** | **225.80** | **+2.04%** | **+14.39%** | **$94,789,308** | **421,167** | **2.09x** |
| 07:10 | 223.37 | -1.08% | +13.16% | $62,239,789 | 277,108 | 1.37x |
| 07:15 | 221.07 | -1.03% | +11.99% | $45,424,578 | 205,266 | 1.00x |


## 2026-06-07 20:00 ET  (overnight)

HL perp move +2.18% at 10.4σ on $560,440 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-05) = $160,208,002 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 19:50 | — | — | — | $0 | 0 | 0.00x |
| 19:55 | — | — | — | $0 | 0 | 0.00x |
| **20:00** | **477.52** | **—** | **—** | **$4,518,238** | **9,569** | **0.03x** |
| 20:05 | 474.36 | -0.66% | — | $4,171,751 | 8,814 | 0.03x |
| 20:10 | 473.00 | -0.29% | — | $3,035,987 | 6,423 | 0.02x |


## 2026-07-23 16:00 ET  (postmarket)

HL perp move +2.18% at 13.3σ on $5,829,473 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-22) = $100,885,915 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 537.40 | — | +0.00% | $162,280,914 | 301,656 | 1.61x |
| 15:55 | 539.57 | +0.40% | +0.40% | $203,188,743 | 376,868 | 2.01x |
| **16:00** | **551.28** | **+2.17%** | **+2.58%** | **$190,844,316** | **347,658** | **1.89x** |
| 16:05 | 556.06 | +0.87% | +3.47% | $78,962,490 | 142,341 | 0.78x |
| 16:10 | 554.77 | -0.23% | +3.23% | $42,243,357 | 75,940 | 0.42x |

