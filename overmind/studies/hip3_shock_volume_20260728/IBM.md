# IBM — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:IBM`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**2 shocks: 1 earnings-linked, 1 other.**

---

# Earnings

## 2026-07-22 16:10 ET  (postmarket)

**EARNINGS 2026-07-22**  ·  HL perp move +2.17% at 6.4σ on $492,132 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-07-21) = $14,764,795 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:00 | 206.75 | — | +0.00% | $1,891,990 | 9,156 | 0.13x |
| 16:05 | 209.92 | +1.54% | +1.54% | $20,700,867 | 99,296 | 1.40x |
| **16:10** | **214.58** | **+2.22%** | **+3.79%** | **$66,916,193** | **312,436** | **4.53x** |
| 16:15 | 211.65 | -1.36% | +2.37% | $23,693,898 | 111,496 | 1.60x |
| 16:20 | 212.61 | +0.45% | +2.83% | $13,563,765 | 64,122 | 0.92x |


---

# Other (non-earnings)

## 2026-06-18 23:45 ET  (overnight)

HL perp move -4.08% at 7.5σ on $547,323 HL notional.  Equity tape: `OCEA.MEMOIR`.  Baseline (Nasdaq regular session, prev day 2026-06-17) = $7,330,721 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 23:35 | — | — | — | $0 | 0 | 0.00x |
| 23:40 | — | — | — | $0 | 0 | 0.00x |
| **23:45** | **—** | **—** | **—** | **$0** | **0** | **0.00x** |
| 23:50 | — | — | — | $0 | 0 | 0.00x |
| 23:55 | — | — | — | $0 | 0 | 0.00x |

