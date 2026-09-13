# RIVN — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:RIVN`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**2 shocks: 1 earnings-linked, 1 other.**

---

# Earnings

## 2026-02-12 16:00 ET  (postmarket)

**EARNINGS 2026-02-12**  ·  HL perp move +9.33% at 20.2σ on $130,554 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-02-11) = $2,763,183 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 15:50 | 13.99 | — | +0.00% | $10,256,987 | 732,762 | 3.71x |
| 15:55 | 13.98 | -0.04% | -0.04% | $13,139,258 | 940,624 | 4.76x |
| **16:00** | **15.15** | **+8.37%** | **+8.33%** | **$9,251,295** | **618,034** | **3.35x** |
| 16:05 | 15.63 | +3.16% | +11.76% | $11,295,498 | 725,080 | 4.09x |
| 16:10 | 15.80 | +1.09% | +12.97% | $8,196,294 | 519,932 | 2.97x |


---

# Other (non-earnings)

## 2026-03-19 08:00 ET  (premarket)

HL perp move +2.57% at 7.1σ on $158,419 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-18) = $2,118,659 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 07:50 | — | — | — | $0 | 0 | 0.00x |
| 07:55 | 16.53 | — | — | $2,111,261 | 127,723 | 1.00x |
| **08:00** | **17.22** | **+4.17%** | **—** | **$10,134,150** | **594,572** | **4.78x** |
| 08:05 | 16.99 | -1.34% | — | $3,986,676 | 234,636 | 1.88x |
| 08:10 | 17.13 | +0.82% | — | $3,721,423 | 218,434 | 1.76x |

