# NFLX — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:NFLX`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**3 shocks: 2 earnings-linked, 1 other.**

---

# Earnings

## 2026-01-20 16:00 ET  (postmarket)

**EARNINGS 2026-01-20**  ·  HL perp move -3.85% at 9.0σ on $583,009 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-01-16) = $21,076,352 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 87.73 | — | +0.00% | $140,367,319 | 1,596,926 | 6.66x |
| 15:55 | 87.57 | -0.18% | -0.18% | $199,451,997 | 2,275,735 | 9.46x |
| **16:00** | **83.94** | **-4.15%** | **-4.32%** | **$87,316,420** | **1,030,157** | **4.14x** |
| 16:05 | 82.90 | -1.23% | -5.51% | $140,063,209 | 1,675,551 | 6.65x |
| 16:10 | 83.66 | +0.92% | -4.64% | $119,761,522 | 1,439,787 | 5.68x |


## 2026-07-16 16:00 ET  (postmarket)

**EARNINGS 2026-07-16**  ·  HL perp move -7.58% at 6.5σ on $7,000,135 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-15) = $16,103,560 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 73.92 | — | +0.00% | $91,719,641 | 1,241,916 | 5.70x |
| 15:55 | 74.35 | +0.58% | +0.58% | $166,534,409 | 2,245,402 | 10.34x |
| **16:00** | **72.46** | **-2.55%** | **-1.98%** | **$64,600,724** | **870,163** | **4.01x** |
| 16:05 | 70.35 | -2.91% | -4.83% | $106,339,363 | 1,507,268 | 6.60x |
| 16:10 | 68.93 | -2.02% | -6.75% | $75,040,453 | 1,074,567 | 4.66x |


---

# Other (non-earnings)

## 2026-02-26 17:50 ET  (postmarket)

HL perp move -2.87% at 6.6σ on $260,353 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-25) = $39,211,655 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 17:40 | 84.82 | — | +0.00% | $1,678,599 | 19,690 | 0.04x |
| 17:45 | 95.11 | +12.13% | +12.13% | $95,889,771 | 1,036,681 | 2.45x |
| **17:50** | **92.50** | **-2.74%** | **+9.05%** | **$106,351,220** | **1,145,815** | **2.71x** |
| 17:55 | 93.01 | +0.55% | +9.66% | $98,186,194 | 1,057,481 | 2.50x |
| 18:00 | 93.01 | -0.00% | +9.66% | $47,236,289 | 508,920 | 1.20x |

