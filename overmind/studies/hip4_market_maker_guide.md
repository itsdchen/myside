# HIP-4 for market makers — onboarding guide

Written 2026-07-31. Short, practical companion to
[`hip4_guide.md`](hip4_guide.md) (full mechanics) and
[`hip4_deployer_analysis.md`](hip4_deployer_analysis.md) (settlement risk).

Audience: someone who already makes markets in perps or spot and wants to quote
outcomes. This covers **what breaks** when you carry those instincts over.

**[measured]** = we probed it. **[analysis]** = reasoning, not observation.

---

## 0. The opportunity, stated numerically

Mainnet daily binaries, measured 2026-07-31 ~6h after deployment: **[measured]**

| Market | mid | spread | rel. spread | bid depth | ask depth |
|---|---|---|---|---|---|
| BTC `#9690` | 0.02216 | 0.00432 | **19.5%** | $288 | $73 |
| ETH `#9700` | 0.07449 | 0.06898 | **92.6%** | $316 | $1.24M |
| SOL `#9710` | 0.13106 | 0.05788 | **44.2%** | $411 | $1.24M |
| HYPE `#9720` | 0.25072 | 0.03855 | **15.4%** | $875 | $1.24M |
| — BTC perp | 62733.5 | 1.0 | **0.0016%** | — | — |

Relative spreads are **four to five orders of magnitude wider than the perp on
the same venue**, and this is not a tick constraint: at 0.022 the 0.00001 tick
is 0.045% of mid, so that 19.5% spread is **~432 ticks wide** where the venue
permits 1. **[measured]**

Bid-side depth in the wings is a few hundred dollars. The huge ask figures are
the mirror of someone's deep No bids (§3 of the main guide) — passive tail
selling, not two-sided quoting.

Read that as: **almost nobody is making these markets.** Read it also as a
warning — spreads that wide usually mean the risk is genuinely hard, and §3
below is why.

---

## 1. Capital replaces margin as the binding constraint

The single biggest adjustment. On perps you think in margin and leverage; here
there is none.

- Every position is **1:1 collateralised**. To be short 1000 Yes you either buy
  1000 No at `(1−p)` each, or lock 1000 quote tokens via `splitOutcome` and sell
  the Yes leg.
- There is **no borrow**, so you cannot sell what you don't hold — the venue
  rejects it outright with `Insufficient spot balance`. **[measured]**
- Your inventory turnover is capped by capital, not by margin. `mergeOutcome`
  is your release valve: it burns paired Yes+No back into quote tokens without
  waiting for settlement, and `amount: null` merges only `min(Yes, No)`, so it
  can never touch a directional leg. **Call it on a schedule.** **[measured]**

Practical: size your book in *capital deployed*, not notional. A perp MM running
20× thinks nothing of $10M notional on $500k. Here $10M of outcome inventory is
$10M locked.

---

## 2. Queue priority depends on which side you are, not just price and time

Yes and No share **one book** — buying Yes at `p` *is* selling No at `1−p`. But
priority is **price-side-time**, not price-time: at the same merged level,
resting *sells* rank ahead of resting *dual buys*, even ones that arrived
earlier. **[docs]**

So the two economically identical ways to quote the same level are **not**
equally good:

| Route | Requires | Cost/share | Queue |
|---|---|---|---|
| Buy No at `1−p` | `(1−p)` quote | `1−p` | behind |
| Split, sell Yes at `p` | 1 quote, returns `p` | `1−p` | **ahead** |

**Getting the good side of the queue costs you a split.** That is the whole
reason the primitive exists. Budget for it: you are paying capital turnover for
fill priority, and on a book this wide, priority is most of the edge.

---

## 3. The risk that will actually hurt you: digital gamma

This is where perp intuition fails hardest, and it is why the spreads are wide.

A binary's sensitivity to the underlying is `∂P/∂S ≈ n(d₂)/(S·σ·√T)`. As
`T → 0` with spot near the strike, **that blows up without bound.** **[analysis]**

Concretely: an hour before settlement with BTC sitting on the strike, your
position is a coin flip that resolves 0 or 1 on the next mark tick. There is no
delta you can hold that hedges it, because the payoff is discontinuous. Perps
have constant delta 1 and you can always trade out; a digital at the strike at
expiry is genuinely unhedgeable.

Three compounding factors specific to HIP-4:

1. **Settlement keys off two mark ticks** straddling 06:00 UTC, linearly
   interpolated (§6 of the main guide). Your fate is decided by a two-tick
   window. **[docs]**
2. **All four majors settle at the same instant.** BTC, ETH, SOL and HYPE
   digitals all resolve at 06:00, and they are the same crypto beta. This is a
   *concentrated, correlated* event, not a diversified book. **[analysis]**
3. **Open orders cancel at settlement** — you cannot quote through it.

**The rule that follows: be flat, or be deliberately positioned, into 06:00.
Do not be passively long gamma at the strike.**

---

## 4. Theta is real and directional

Perp carry is funding — small, roughly symmetric, mean-reverting. Outcome carry
is **option theta**: an OTM contract decays toward 0, an ITM one accretes
toward 1. **[analysis]**

Holding inventory is not neutral. A 0.02 Yes position bleeds to zero unless
you're right, and the decay accelerates into expiry. If you're used to carrying
perp inventory overnight at near-zero cost, this is a different animal — your
inventory has a clock on it and the clock has ~24 hours on the dailies.

---

## 5. Sizing is discrete, and it kills the wings

Two hard rules, both measured: **[measured]**

- **Whole shares only** (`szDecimals = 0`). `size=40.5` → `Order has invalid size.`
- **10 USDC minimum order value**, computed on the *premium* (`px × sz`), not
  the payout.

Combined, the minimum share count scales as **`10/p`**:

| Price | Min shares | Min order |
|---|---|---|
| 0.50 | 20 | $10 |
| 0.10 | 100 | $10 |
| 0.02 | 500 | $10 |
| 0.005 | 2000 | $10 |

You **cannot** make a small market in the wings. At 0.02 your smallest possible
quote is 500 shares, and the increment is 50 shares ($1). This mechanically
thins deep-OTM liquidity and is a large part of why the BTC book above shows
$288 of bids at 0.022. **[analysis]**

It also means the contract you're quoting changes character during the day:
**it is ATM only at 06:00 and drifts into the wings within hours** — the BTC
digital was already at 0.022 six hours in. **[measured]** Plan for quoting a
wing most of the time, not a straddle.

---

## 6. Hedge with the underlying perp — same venue

The dual side is *not* a hedge; it's the same book. To hedge an outcome you
trade the **underlying perp**, sized by the digital's delta. **[analysis]**

This is a genuine structural advantage of HIP-4 living on HyperCore: the BTC
perp and the BTC digital are on the same exchange with the same price feed, and
the digital settles against that exact mark. Basis risk between hedge and
instrument is close to nil — unusual for a prediction market.

Caveat: they sit in **different clearinghouses**. Outcome inventory is in the
spot clearinghouse; the perp hedge is in the perp clearinghouse with its own
margin. You will be moving collateral between them, and the hedge ratio explodes
near expiry (§3), so the hedge stops working exactly when you need it most.

---

## 7. Operational checklist

Things that will silently break a bot: **[measured]**

1. **Re-seed the asset map on every daily roll.** Recurring ids advance in a
   block of 8 per day; yesterday's are dead. A stale seed throws
   `KeyError: '#…'` on every order. Subscribe `outcomeMetaUpdates` and re-seed
   on `outcomeCreated`.
2. **Do not compute PnL naively from `userFills`.** The collateral primitives
   emit synthetic fills at 1/N prices with `oid` populated. They net to zero in
   aggregate, but **selectively filtering `dir` breaks the netting** and books
   phantom PnL. Either take all legs or exclude all four and reconcile against
   `spotClearinghouseState`.
3. **Price tick is a fixed 0.00001 — 5 decimals, not 5 significant figures.**
   The spot sig-fig rule silently produces invalid prices below 0.1. We shipped
   that bug.
4. **Check `deployer` before quoting size.** Null = protocol-settled,
   deterministic. Non-null = a third party chooses the settlement fraction by
   hand with no documented dispute path. `hip4_utils.py` warns on this.
   Currently every mainnet outcome is protocol-settled.
5. **Assert `Yes_mid + No_mid == 1`** continuously as a feed integrity check,
   and watch for `Yes_ask + No_ask < 1` (buy both → merge → riskless) or
   `Yes_bid + No_bid > 1` (split → sell both). These should never open.
6. **Read `quoteToken` per market.** Mainnet recurring is USDC; testnet mixes
   USDH and USDC.

---

## 8. What's genuinely better than perps

Worth stating, because the list above is all friction:

- **Bounded, known worst case.** Max loss on a buy is `p × size`. No liquidation,
  no gap risk, no funding surprises, no cascade. You can size to a hard floor.
- **No liquidation engine means no liquidation games** — none of the stop-hunt
  or cascade dynamics that dominate leveraged perp microstructure.
- **Mechanically enforced no-arb.** `Yes + No = 1` is guaranteed by split/merge,
  not by arbitrageurs. The basis cannot drift.
- **Zero fees today** — verified on a genuine crossed fill. Explicitly temporary,
  so do not build an edge that only survives at 0 bps.
- **A near-perfect hedge instrument on the same venue** (§6).

---

## 9. Honest assessment

The spreads are enormous and almost nobody is quoting. That is either a large
opportunity or a correctly-priced reflection of unhedgeable pin risk at 06:00 —
and on the evidence so far, **it is probably some of both**. The wings are wide
partly because of a structural constraint (min notional `10/p`) that no amount
of competition can fix.

Before committing capital, the thing to establish is §6 of the main guide's open
item: does the book's implied vol differ systematically from your own surface,
strike-aware across the day? That is answerable from public candle and
settlement history with no venue access and no capital at risk. Do that first.
