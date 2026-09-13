# USAR — shock volume study

Shocks detected on the Hyperliquid perp (`xyz:USAR`), profiled against US equity 1-minute bars: Nasdaq Basic (`XNAS.BASIC`) during 04:00-20:00 ET, Blue Ocean (`OCEA.MEMOIR`) overnight. Each event header names the tape it used.

- Notional = Σ (volume × close) per 1-min bar, aggregated to 5-minute buckets.
- The 3 minutes centred on 09:30 and 16:00 are excluded everywhere (opening/closing crosses).
- `vs base` = multiple of the previous trading day's 09:32–15:58 average notional per 5-min bucket.
- The **bold** row is the shock bucket.

**4 shocks: 1 earnings-linked, 3 other.**

---

# Earnings

## 2026-05-13 16:35 ET  (postmarket)

**EARNINGS 2026-05-13**  ·  HL perp move -4.05% at 5.6σ on $87,119 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-05-12) = $3,057,715 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 16:25 | 25.10 | — | +0.00% | $451,743 | 18,132 | 0.15x |
| 16:30 | 26.79 | +6.73% | +6.73% | $2,527,020 | 95,713 | 0.83x |
| **16:35** | **25.80** | **-3.70%** | **+2.79%** | **$3,388,288** | **128,797** | **1.11x** |
| 16:40 | 25.97 | +0.67% | +3.48% | $676,168 | 26,087 | 0.22x |
| 16:45 | 26.00 | +0.10% | +3.59% | $659,867 | 25,450 | 0.22x |


---

# Other (non-earnings)

## 2026-03-23 07:10 ET  (premarket)

HL perp move +3.11% at 8.3σ on $29,560 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-03-20) = $1,769,213 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 07:00 | 15.63 | — | +0.00% | $11,944 | 764 | 0.01x |
| 07:05 | 16.32 | +4.41% | +4.41% | $241,696 | 15,013 | 0.14x |
| **07:10** | **16.70** | **+2.33%** | **+6.85%** | **$118,201** | **7,112** | **0.07x** |
| 07:15 | 17.08 | +2.28% | +9.28% | $92,688 | 5,457 | 0.05x |
| 07:20 | 16.90 | -1.05% | +8.13% | $262,659 | 15,469 | 0.15x |


## 2026-06-03 08:20 ET  (premarket)

HL perp move +3.03% at 8.1σ on $166,756 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-02) = $3,796,166 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 08:10 | 29.99 | — | +0.00% | $24,550 | 819 | 0.01x |
| 08:15 | 31.70 | +5.69% | +5.69% | $6,090,604 | 191,685 | 1.60x |
| **08:20** | **32.64** | **+2.97%** | **+8.84%** | **$6,315,382** | **195,925** | **1.66x** |
| 08:25 | 32.14 | -1.53% | +7.17% | $4,491,833 | 139,550 | 1.18x |
| 08:30 | 32.46 | +0.99% | +8.23% | $2,378,285 | 73,679 | 0.63x |


## 2026-06-17 06:40 ET  (premarket)

HL perp move +2.47% at 6.6σ on $65,857 HL notional.  Equity tape: `XNAS.BASIC`.  Baseline (Nasdaq regular session, prev day 2026-06-16) = $1,991,879 per bucket.

| Time | Close | Ret | Cum | Notional | Shares | vs base |
|---|---:|---:|---:|---:|---:|---:|
| 06:30 | — | — | — | $0 | 0 | 0.00x |
| 06:35 | 22.07 | — | — | $89,448 | 4,053 | 0.04x |
| **06:40** | **22.70** | **+2.85%** | **—** | **$249,697** | **11,031** | **0.13x** |
| 06:45 | 22.47 | -1.02% | — | $7,976 | 354 | 0.00x |
| 06:50 | 22.33 | -0.62% | — | $136,797 | 6,110 | 0.07x |

