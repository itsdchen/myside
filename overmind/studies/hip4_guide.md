# HIP-4 outcome markets — working guide

Written 2026-07-30, consolidated 2026-07-31. Companion to
`overmind/strat_main/tools/hip4_utils.py`. Read-side probed against mainnet;
every write action verified end-to-end on testnet with the account
`0x2AD7...a6b9`.

This is the single source of truth for the **taker** side. It absorbs the
earlier `hip4_observations.md` reconnaissance notes, which have been deleted —
several of their conclusions (notably the tick rule) turned out to be wrong and
are corrected here.

Companion docs:
- **[`hip4_sports_deployer_context.md`](hip4_sports_deployer_context.md) — START
  HERE if you are picking up the sports-deployment work.** Orientation, blockers,
  and the reference implementation.
- [`hip4_deployer_analysis.md`](hip4_deployer_analysis.md) — the deployer side:
  market creation, settlement authority, and the void problem.
- [`hip4_market_maker_guide.md`](hip4_market_maker_guide.md) — what changes
  coming from perp/spot market making; structural, plus a dated spread snapshot.

Status tags: **[verified]** = we did it and watched it work. **[docs]** = from
the Hyperliquid docs, not independently confirmed. **[assumed]** = our
inference, could be wrong.

---

## 1. The mental model

An outcome is a **fully collateralised binary token**. Not a perp. There is no
leverage, no funding, no liquidation, no margin, and no way to lose more than
you put in.

- Each market has two sides, each side is a token. Price is a probability in
  (0, 1). One Yes share pays exactly 1 quote token if the event resolves Yes,
  0 otherwise.
- Buy at `p`: max loss `p × size`, max gain `(1−p) × size`. That's the whole
  risk model.
- Positions live in the **spot** clearinghouse. `hl_utils.py --mode balance`
  (perp clearinghouse) will never show them. **[verified]**
- Settlement converts Yes → `settleFraction` quote tokens, No →
  `1 − settleFraction`. **Protocol** markets use exactly "1.0" or "0.0";
  deployer markets need not — see §4c. **[verified]**

The single most important structural fact: **1 Yes + 1 No is always worth
exactly 1 quote token**, at any time, in any state of the world. The protocol
will mint or burn that pair for you on demand (§4). Everything else follows
from this.

Practical consequence for us: **none of our HIP-3 margin or liquidation
machinery applies**, and none of our perp risk plumbing will pick these up.
Separate book, separate collateral model, separate clearinghouse.

---

## 2. The encoding — three skins, one integer

```
encoding = 10 × outcome + side          # side 0 = first sideSpec (usually Yes)
                                        # side 1 = second (usually No)
```

That integer then appears three different ways depending on the endpoint: **[verified]**

| Skin | Where | Example (outcome 961, side 0) |
|---|---|---|
| `#<encoding>` | l2Book, recentTrades, candleSnapshot, allMids, openOrders | `#9610` |
| `+<encoding>` | spotClearinghouseState balance rows | `+9610` |
| `100_000_000 + encoding` | integer `asset` in signed order/cancel actions | `100009610` |

This is a **fourth** asset-id scheme in the venue, alongside core perps (`0..`),
spot (`10000 + idx`), and HIP-3 builder perps (`100000 + dex×10000 + idx`).
The base is 100 **million** — one digit off from the HIP-3 base and easy to
misread.

Confirmed live: an order on `#102180` came back with `asset=100102180` in the
venue's own error string. **[verified]**

Use the helpers in `hip4_utils.py` (`encode_outcome`, `outcome_coin`,
`outcome_token`, `outcome_asset_id`, `parse_outcome_sym`) rather than building
these strings by hand.

---

## 3. The merged book

Yes and No for one outcome are **one book, mirrored about 1.0**. Buying Yes at
`p` *is* selling No at `1−p`. **[verified]** — live mainnet outcome 961:

```
#9610  bids: 0.86501  0.865   0.86039 …   asks: 0.88    0.88299  0.883  …
#9611  bids: 0.12     0.11701 0.117   …   asks: 0.13499 0.135    0.13961 …
```

`#9611` best bid 0.12 = 1 − 0.88 (`#9610` best ask), and this holds at every
level. Mids for the two sides sum to exactly 1.00000 across all live markets.

Consequences:

- **There is no Yes/No arbitrage.** They are the same liquidity. Do not build one.
- Priority is **price-side-time**, not price-time: at the same merged price
  level, resting sells sort ahead of resting dual buys. **[docs]** Concretely,
  a "buy Yes at `p`" sits behind a "sell No at `1−p`" that arrived *later*.
  This is not a curiosity — it is the entire reason `splitOutcome` is worth
  using (see §4).
- An order that both crosses and rests can report split across the primary and
  dual coin in order history. **[docs]** So one submitted order ≠ one
  `openOrders` row. Docs say this will be cleaned up in a later upgrade.

---

## 4. The collateral primitives — all four verified

These are what make outcomes tradeable without borrowing. All are
`{"type": "userOutcome", "<op>": {...}}` L1 actions, amounts as strings.
**All four verified on testnet with exact balance reconciliation.**

### `splitOutcome` — mint a pair
X quote tokens → X Yes + X No. **[verified]**
```
USDC 44562.909 → 44552.909, +102180: 10.0, +102181: 10.0     (split 10)
```
Manufactures inventory so you can rest **sell** orders — see the queue-priority
discussion below for why that's the real reason to use it.

Rejections measured: **[verified]**
- fractional amount (`10.5`) → `Invalid input number` — whole units only,
  consistent with szDecimals = 0
- more than your quote balance → `Outcome error: Insufficient quote token balance`

Both fail cleanly with no partial state change.

### `mergeOutcome` — burn a pair
X Yes + X No → X quote tokens. **[verified]**
```
merge 4    → USDC 44556.909, +102180: 6.0,  +102181: 6.0
merge null → USDC 44562.909, +102180: 0.0,  +102181: 0.0
```
`amount: null` means **`min(Yes, No)`**, not "everything" — confirmed against
asymmetric holdings: with 20 Yes and 40 No, `merge null` burned exactly 20 and
left 20 No standing. **[verified]** So it is always safe to call: it can never
touch an unpaired directional position, only the flat residue.

Sweeps a paired residual back to cash without waiting for settlement.

### `negateOutcome` — relabel within a question
X No of one outcome → X Yes of **every other outcome in the question**.
**[verified]** on question 820 (May CPI, 3-way + fallback):
```
before: +102180: 20 (Yes 10218), +102181: 20 (No 10218)
after:  +102170: 20, +102180: 20, +102190: 20, +102200: 20
```
"Not 10218" ≡ "10217 or 10219 or 10220". **Note the fallback outcome (10217)
is included** — the docs don't spell this out, but it must be, or the identity
wouldn't hold. Use it to move a No position onto whichever book has liquidity.

### `mergeQuestion` — redeem a full set
X Yes of *every* outcome in a question → X quote tokens. **[verified]**
```
before: +102170: 20, +102180: 20, +102190: 20, +102200: 20
after:  USDC back to 44562.9091412 — exactly the starting balance
```

**The full round trip `split → negate → mergeQuestion` returned the starting
USDC to the last decimal.** That confirms both the wire formats and that fees
are genuinely zero right now.

### ⚠ The primitives emit SYNTHETIC FILLS with fabricated prices and PnL

**This will corrupt any fill-based PnL or risk system that isn't told about
it.** All four collateral operations show up in `userFills` alongside genuine
trades, tagged by `dir`: **[verified]**

```
dir="Split Outcome"    2 legs,  px 0.5        each
dir="Merge Outcome"    2 legs,  px 0.5        each
dir="Negate Outcome"   N legs,  px 0.33333333 each + source leg at px 1.0
dir="Merge Question"   4 legs,  px 0.25       each
```

The `px` is **1/N where N is the leg count** — a pure accounting convention.
It is *not* a market price and bears no relation to where the outcome is
trading. Two tells that these aren't order-book events:

- `px: "0.33333333"` carries **8 decimals**, violating the 5-decimal tick that
  every real order must satisfy (§5).
- They still populate `oid` and set `crossed: true`, so **the presence of an
  `oid` does not mean an order was placed.**

**`closedPnl` is internally consistent — but only if you take ALL the legs.**
Individual legs look alarming. Our `split(20) → negate → mergeQuestion(20)`
round trip was economically flat to the last decimal, yet reports:

```
dir="Negate Outcome"  #102181  px 1.0   closedPnl  +10.0
dir="Merge Question"  #102180  px 0.25  closedPnl   -5.0
dir="Merge Question"  #102170  px 0.25  closedPnl   -1.6666666
dir="Merge Question"  #102190  px 0.25  closedPnl   -1.6666666
dir="Merge Question"  #102200  px 0.25  closedPnl   -1.6666666
                                        ------------------------
                                 total:        exactly 0.0000
```

**They sum to zero, correctly.** `split` assigns a 0.5 basis to each leg (fair:
you paid 1.0 for the pair). `negate` consumes a No carrying 0.5 basis and
credits it at px 1.0, booking +10. `mergeQuestion` then unwinds the four legs
at 0.25 and books −10. Net zero, matching reality.

So the real trap is **not** that the numbers are fake — it's that they are
**only correct in aggregate**:

- ✅ Summing `closedPnl` across *all* outcome fills gives the right answer.
- ❌ Reading any *single* leg's `px` or `closedPnl` as an economic price or PnL
  is meaningless — `px` is a 1/N convention.
- ❌ **Filtering `dir` selectively silently breaks the netting.** Drop
  `Merge Question` as "not a real trade" while keeping `Negate Outcome` and you
  book a **+10 phantom gain**. Do it the other way and you book −10.

If you exclude the synthetic directions, you must exclude **all four** and
reconcile against `spotClearinghouseState` balances instead. Partial filtering
is worse than no filtering.

Genuine trades interleave cleanly: our taker sell of 20 Yes at 0.6 against a
0.5 split basis reported `dir="Sell"`, `closedPnl +2.0`, offset by −2.0
unrealized on the retained No leg. Consistent.

`feeToken` also varies by direction (the outcome token `+102181` on split and
negate, `USDC` on merges), which is another reason not to treat these
uniformly with real fills.

### Why `split` exists at all — the queue-priority answer

Given the merged book, split looks redundant for directional trading, and
mostly is. "Sell Yes at p" and "buy No at 1−p" are the same trade at the same
cost, and buying No needs no split. So why does the primitive exist?

First, a hard constraint we measured: **you cannot sell what you don't hold.**
A sell of 20 Yes with a zero Yes balance is rejected outright — the venue does
*not* auto-split you into the dual position: **[verified]**
```
'Insufficient spot balance asset=100102180'
```

So the two routes to the same economic position differ in what they require:

| Route | Needs | Cost per share |
|---|---|---|
| Buy No at `1−p` | `(1−p)` USDC | `1−p` |
| Split, then sell Yes at `p` | 1 USDC, returns `p` | `1−p` |

Identical economics, one extra action. **The difference is queue priority.**
Per §3, priority is **price-side-time**: at the same merged price level,
resting *sells* sort ahead of all resting *dual buys*. Selling Yes at `p`
therefore outranks buying No at `1−p` in the queue — but you can only rest that
sell if you hold inventory, and holding inventory requires a split.

**That is what `split` buys you: the priority-advantaged side of the book.**
For a passive strategy that's the whole game, and it costs a round trip through
collateral to get. Beyond that, `merge` is a capital-efficiency operation —
releases the 1 USDC per pair tied up in a flat position without waiting for
settlement — and `negate`/`mergeQuestion` do genuine cross-market work the
order book cannot express.

### The no-arb band these imply
```
Yes_ask + No_ask < 1   → buy both, mergeOutcome, riskless quote token
Yes_bid + No_bid > 1   → splitOutcome, sell both, riskless quote token
```
Because the book is merged these should never open — but they're the correct
assertions to run continuously on the feed as a data-integrity check.

---

## 4b. Multi-outcome questions — the N-leg case (A / B / TIE)

Everything above is the 2-side case. A 3-outcome market behaves differently
enough to deserve its own treatment, and there is one trap in it that does not
exist in the binary case.

### It's 4 outcomes, not 3

`A wins / B wins / TIE` deploys as a **question with 3 named outcomes plus an
auto-created fallback** — 4 outcomes, 8 tokens, **4 separate merged books**.
The fallback counts against the deployer's active-outcome cap and it is a real,
tradeable market with its own price. **[docs + verified]**

Exactly one of `{A, B, TIE, F}` settles to 1, so:

```
p_A + p_B + p_TIE + p_F = 1
```

### Position algebra

Per leg, `nᵢ = Yᵢ − Nᵢ`. Given winner `w`, total payout is:

```
Σᵢ [ Yᵢ·1(i=w) + Nᵢ·1(i≠w) ]  =  ΣᵢNᵢ  +  n_w
```

`ΣNᵢ` is a constant — known, outcome-independent. **All your exposure is `n_w`,
the net position on whichever leg happens to win.**

Two consequences that break perp intuition:

- **"Flat" is the equal vector, not the zero vector.** If every `nᵢ = k`, the
  payout is `ΣNᵢ + k` regardless of who wins. You are flat holding a large
  position, provided it's evenly spread.
- **Risk is the spread of the vector, not its magnitude.** An N-outcome question
  has **N−1 independent risk dimensions**, not N. For A/B/TIE/F that's 3 — or 2
  if the fallback is pinned at zero.

The collateral primitives move you **along the diagonal**, which is the riskless
direction:

```
split + negate    →  +1 to every nᵢ,  costs 1 quote token
mergeQuestion     →  −1 to every nᵢ,  releases 1 quote token
```

Both are **flatness-preserving**. Trading is what moves you off the diagonal.
Capital released by `mergeQuestion` is `minᵢ(Yᵢ)`.

### The mental model: tokens are claims on subsets of states

Do not memorise the four primitives. Derive them.

A match has **4 states of the world**: A wins, B wins, TIE, Fallback. Every
token is just *"pays $1 in this subset of states"*:

| Token | Pays in |
|---|---|
| `Yes_A` | {A} |
| `Yes_B` | {B} |
| `Yes_TIE` | {TIE} |
| `Yes_F` | {F} |
| `No_A` | {B, TIE, F} |
| cash | {A, B, TIE, F} |

The four `Yes` tokens are **atoms** — one per state. Everything else is a sum of
atoms. `No_A` is not a distinct instrument; it is literally
`Yes_B + Yes_TIE + Yes_F` bundled.

Now every primitive is set arithmetic on the state space:

```
splitOutcome     cash → Yes_A + No_A           {ABTF} = {A} + {BTF}
mergeOutcome     Yes_A + No_A → cash           the reverse
negateOutcome    No_A → Yes_B+Yes_T+Yes_F      {BTF} = {B}+{T}+{F}
mergeQuestion    all four Yes → cash           {A}+{B}+{T}+{F} = {ABTF}
```

**Any complete partition of the states can be assembled into cash, or cash
dissolved into any partition** — because a complete partition pays exactly $1 in
every state, so the protocol will always mint or burn it.

This also explains an apparent gap: **there is no `splitQuestion`, and none is
needed.** `split` then `negate` composes into it:

```
cash → Yes_A + No_A → Yes_A + Yes_B + Yes_TIE + Yes_F
```

which is exactly the sequence verified on testnet. **[verified]**

### Reading a position: the atom vector

Rewrite every `No` as its basket and you get a pure atom vector — your payout in
each state:

```
aᵢ = Yesᵢ + Σ(No_j for j≠i)          "everything that pays when i wins"
```

Settlement pays `a_w` for whichever state `w` occurs, so **the atom vector *is*
your payout-by-state vector.** It's the same quantity as `ΣNᵢ + nᵢ` above, just
in a form you can read directly. Four numbers describe the whole position:

- **flat** ⟺ all `aᵢ` equal
- **risk** ⟺ the spread between them
- `mergeQuestion` lowers all four equally → travels the flat direction

### Worked example — one full market-making cycle

Fair prices `p_A = 0.50`, `p_B = 0.25`, `p_TIE = 0.25`, `p_F = 0.00` (sum 1.00).
Start with $1000 cash and no tokens.

**1 — Acquire a full set.** `splitOutcome(A, 100)` then `negateOutcome(q, A, 100)`:

```
after split:   cash 900   Yes_A 100, No_A 100          atoms (100,100,100,100)
after negate:  cash 900   Yes_A/B/T/F 100 each         atoms (100,100,100,100)
```

Cost $100, and the position is **flat** at every step — you hold size but earn
the same $100 whoever wins. Total worth still $1000. This is the diagonal: it
costs exactly 1 per unit and carries no risk.

**2 — Sell all four legs** at a 4% overround (0.51 / 0.26 / 0.26 / 0.01, sum 1.04):

```
cash 900 + 104 = 1004      all tokens 0      atoms (0,0,0,0)
```

**Profit $4, risk-free.** That is `(Σ offers − 1) × 100` — a bookmaker's
balanced book, expressed in protocol primitives.

**3 — The realistic case: only A and TIE get lifted.**

```
cash 900 + 51 + 26 = 977   Yes_B 100, Yes_F 100        atoms (0, 100, 0, 100)
```

Not flat. You paid 100 for the set and recovered 77, so:

| If … wins | A | B | TIE | F |
|---|---|---|---|---|
| total value | 977 | 1077 | 977 | 1077 |

Long B and F, short A and TIE, spread 100. **This imbalance is the entire MM
problem** — the overround is only risk-free when the whole set clears.

**4 — Rebalancing has two routes.** To lift `a_A` and `a_TIE` you can buy those
legs directly, or buy `No_B`, since `No_B ≡ Yes_A + Yes_TIE + Yes_F`:

```
buy the basket:  0.51 + 0.26 + 0.01 = 0.78
buy No_B:        offered at 0.74     = 0.74     ← cheaper, take it
```

Identical payoff, different price. **Always price both routes.** When the gap is
big enough it stops being a routing choice and becomes the cross-leg arb below.

**5 — Release capital without changing risk.** Say you end up holding
`Yes_A 30, Yes_B 100, Yes_TIE 40, Yes_F 100` → atoms `(30, 100, 40, 100)`.
`mergeQuestion(30)` burns 30 of each and returns $30:

```
atoms (30,100,40,100)  →  (0,70,10,70)      cash +30
spread 70              →  spread 70          unchanged
```

Payout-plus-cash is identical in every state, but $30 of capital is freed.
`minᵢ(aᵢ)` is always dead weight — **run this on a timer.**

### ⚠ The trap: cross-leg no-arb is NOT mechanically enforced

In the binary case `Yes + No = 1` is guaranteed by the matching engine — the two
sides are literally one book, so the band can never open.

**Across the legs of a question, nothing enforces `Σpᵢ = 1`.** Each leg is its
own separate book. The identity holds only because `negateOutcome` lets someone
arbitrage it:

```
No_A  ≡  Yes_B + Yes_TIE + Yes_F        (via negateOutcome)
⇒  1 − p_A = p_B + p_TIE + p_F
```

That is an *arbitrageur-enforced* relationship, not a structural one. So unlike
the binary case, this band genuinely can open:

```
Σᵢ Yesᵢ_ask < 1   → buy one Yes of every leg, mergeQuestion    → riskless
Σᵢ Yesᵢ_bid > 1   → split+negate for a full set, sell all legs → riskless
```

**Monitor this continuously.** In the binary case the equivalent check is a data-
integrity assertion; here it is a live trading signal, and it is the multi-leg
market's most likely source of free money — especially early, when the legs are
quoted by different participants who aren't watching the sum.

### Don't ignore the fallback leg

Per the testnet World Cup reference (§ sports docs), deployers may fold
cancellation into an existing named leg — `Draw resolves Yes if the game is
cancelled` — which pins `p_F ≈ 0` and leaves a clean 3-way. Two caveats:

1. **It still enters the arb sum.** Your `Σpᵢ` check must include it, even at
   zero, or your band is mis-computed.
2. **Cheap tail hedge.** If `F` trades near zero and there is any chance the
   deployer resolves to fallback rather than the folded leg, buying `F` is a
   cheap hedge against settlement ambiguity — which is precisely the
   discretionary risk flagged in the deployer analysis.

### Implication for the strategy/gateway split

The per-leg mapping (`nᵢ = Yᵢ − Nᵢ`, presented as a signed perp position)
generalises to N legs for free — it's the same transform applied N times. What
does **not** generalise is risk:

- A strategy seeing N *independent* instruments will estimate correlations that
  are structural, and will not know it can be flat while holding size.
- It will miss `mergeQuestion` capital release, which `mergeOutcome` cannot
  reach on a multi-leg question.
- It will not see the cross-leg arb above.

This only bites when quoting **more than one leg of the same question at once**.
Single-leg quoting never surfaces it, which is why prototyping against the 2-leg
crypto binaries exercises the same gateway code without forcing the decision.

---

## 4c. `settleFraction` — the payoff is not necessarily binary

Settlement pays Yes → `settleFraction`, No → `1 − settleFraction`. The rules
differ by market type, and this is **the single largest difference between
protocol and deployer markets**: **[docs]**

| Market type | Allowed `settleFraction` |
|---|---|
| Protocol recurring | `"1.0"` or `"0.0"` in practice **[verified]** |
| Outcome belonging to a **question** | must be exactly `"0"` or `"1"` |
| **Standalone** outcome | **any decimal in [0, 1]** — e.g. `"0.66"` |

The standalone case is the "bounded options-like instruments" capability from
the HIP-4 pitch: a standalone outcome can express a **scalar payoff**, not just a
binary. In principle that supports range/index contracts that settle
proportionally rather than to a corner.

### It is used in the wild — as an early void

Testnet has real fractional settlements, and they are instructive. Nine
deployer-created `template:binaryPrice` markets settled at **exactly 0.5**: **[verified]**

```
outcome 10988  perp:xyz:MU|threshold:1000|time:20260814-0000   settleFraction 0.5
outcome 10994  perp:xyz:TSLA|threshold:450|time:20260831-2359  settleFraction 0.5
outcome 10997  perp:xyz:GOLD|threshold:4250|time:20260831-2359 settleFraction 0.5
```

Three things to take from this:

1. **0.5 is the de facto void convention** for standalone outcomes — pay
   everyone half. Same choice Polymarket's UMA makes, with the same flaw: it is
   only a refund for someone who entered at 0.5. A holder who bought at 0.90
   loses 0.40 to the void itself.
2. **They were settled early.** Those expiries are 2026-08-14 and 2026-08-31;
   they were settled before then. This empirically answers an open question —
   **nothing stops a deployer settling before the stated expiry**, and one
   already has.
3. **`details` carried no explanation** — the string was `"template"`. So there
   is no on-chain record of *why* a market was voided.

### What this means for pricing a deployer market

A protocol market is a digital with a known expiry and a known payoff function.
A deployer market is a digital where **both the expiry and the payoff function
are at someone's discretion**.

You are therefore short an option on the deployer's behaviour, and it is not a
small one: they can terminate at any time and pay any fraction. Concretely, any
position not entered at 0.5 carries a jump-to-0.5 risk that is unhedgeable and
untimeable.

**Practical rule:** treat `deployer == null` as a different asset class from
`deployer != null`. Size the second accordingly, or don't quote it.
`hip4_utils.py` warns on this before every order.

Side note on composition: those markets are digitals struck on **HIP-3 perps**
(`xyz:MU`, `xyz:TSLA`, `xyz:GOLD` — equities and gold), via the template's
`hlPerp` keyword. HIP-3 and HIP-4 stack directly.

---

## 5. Order rules — measured, not guessed

| Rule | Value | How we know |
|---|---|---|
| Min order value | **10 USDC** on the premium (`px × sz`) | `Order must have minimum value of 10 USDC. asset=100102180` **[verified]** |
| Size granularity | **whole shares, szDecimals = 0** | `size=40.5` → `Order has invalid size.` **[verified]** |
| Price granularity | **fixed 0.00001 tick — 5 DECIMAL places, not 5 sig figs** | `0.00123` (3sf) rests; `0.012345` (5sf, 6dp) → `Price must be divisible by tick size` **[verified]** |
| Fees | **zero, including on genuine crossed fills** | `fee: "0.0"` on a real taker sell of 20 @ 0.6, and on all collateral-primitive legs **[verified]**. Zero fees are **[docs]** and explicitly temporary |

Two things to note about the minimum. It's on the **premium**, not the payout —
so a 40-share bid at 0.30 (12 USDC) passes, while a 5-share bid at the same
price (1.5 USDC) rejects. And because deep-OTM outcomes trade at 0.02, the
minimum *share count* rises sharply as price falls: at 0.02 you need 500 shares
to clear 10 USDC.

The size rule also means **you cannot scale a position continuously** — the
smallest increment at 0.02 is 500 shares of a 0.02 contract. Position sizing
has to be integer-aware.

### The tick is a uniform grid, and it has to be

Our first reading of the tick rule was wrong and worth recording, because the
mistake is invisible until you quote below 0.1.

Spot and perps use **5 significant figures**. Outcomes use a **fixed 0.00001
tick — 5 decimal places.** Every test we ran initially sat in 0.1–0.9, where
the two rules coincide exactly, so the wrong rule passed. Measured: **[verified]**

| Price | dp / sf | Result |
|---|---|---|
| `0.11234` | 5dp, 5sf | accepted |
| `0.01234` | 5dp, 4sf | accepted |
| `0.00123` | 5dp, 3sf | accepted — a sig-fig rule would allow far more precision |
| `0.012345` | 6dp, 5sf | **rejected** — a sig-fig rule would allow this |
| `0.001234` | 6dp, 4sf | **rejected** |

**It must be a uniform grid because of the merged book.** Buying Yes at `p` is
selling No at `1−p`, so both prices have to be representable. A fixed decimal
grid is closed under `p → 1−p`; a significant-figure rule is not (`0.012345`
is 5sf, but `0.987655` is 6sf). Sig-figs would break the Yes/No mirror at the
extremes — so the tick design follows directly from §3.

The practical bite: deep-OTM is exactly where daily digitals live as expiry
approaches, and that is precisely the region the sig-fig rule corrupts.
`round_outcome_px()` now does `round(px, 5)` and is verified against the venue
at 0.00123, 0.01235 and 0.30123.

---

## 6. Recurring markets — the tradeable structure

Recurring outcomes are deployed and settled **by the protocol**, not a builder.
Ids allocate in a **contiguous block of 8 per day**: **[verified]**

```
expiry 2026-07-30:  953 954 955 956  (BTC/ETH/SOL/HYPE binaries)
                    957              (fallback)
                    958 959 960      (BTC price buckets)
expiry 2026-07-31:  961 962 963 964  965  966 967 968
```

**The new daily binary strikes ATM at the previous settlement mark.** Verified
across all four underlyings on **two consecutive rolls** — 8/8 exact matches: **[verified]**

| Roll | Underlying | prior settle | new target |
|---|---|---|---|
| 07-30 → 07-31 | BTC | 64009 | 64009 |
| | ETH | 1905.36 | 1905.4 |
| | SOL | 73.6702 | 73.672 |
| | HYPE | 53.777 | 53.777 |
| 07-31 → 08-01 | BTC | 64330 | 64330 |
| | ETH | 1905.2 | 1905.2 |
| | SOL | 74.2085 | 74.208 |
| | HYPE | 55.189 | 55.189 |

(new target = prior settle, rounded to 5 sig figs)

**This resolves an ambiguity worth recording.** One roll cannot distinguish
"target = prior settlement value" from "target = mark sampled at deployment,"
because deployment happens *at* the settlement instant. But the settlement
value is an *interpolation* between the two mark ticks straddling 06:00, so it
generally equals no actual mark tick. An independently sampled mark would
essentially never match it to 5 significant figures — and SOL is the clean
tell: settle `74.2085` carries a digit that the target `74.208` rounds off.
Eight exact matches, including that rounding, means **the target is derived
from the settlement computation itself**, not sampled separately.

Bucket thresholds are **±2% around that same reference**, confirmed on three
questions: **[verified]**
```
64009 → 62728 / 65289    (×0.98 = 62729, ×1.02 = 65289)
64330 → 63043 / 65616    (×0.98 = 63043, ×1.02 = 65617)
```

### ATM at deployment only — it drifts fast

The contract is at-the-money *at 06:00 and never again*. Six hours into the
08-01 contract, with BTC struck at 64330, the Yes side was already trading
**0.022** — the market pricing a 2% chance of finishing above the strike.
**[verified]**

So "daily ATM digital" describes the deployment instant, not the tradeable
life of the contract. Most of the day you are trading a wing, not a straddle,
and the 10 USDC minimum bites hard out there (§5): at 0.022 you need 455 shares
to place an order at all.

So every day at 06:00 UTC you get a fresh, exactly-at-the-money 24-hour digital
on each of four majors. Same structure, same horizon, same strike convention,
every single day. That repeatability is the interesting part — a digital is
∂C/∂K, so an existing vol surface prices it directly, and the question is
whether the book's implied vol is systematically off ours.

**Settlement rule** **[docs]**: settles Yes iff the linearly-interpolated mark
across the settlement timestamp is ≥ target:
```
markPx0 + (settleTime − t0)/(t1 − t0) × (markPx1 − markPx0) ≥ targetPrice
```
where t0/t1 are the mark updates immediately before/after 06:00 UTC. Settlement
is decided by **two specific mark ticks** — a narrow window and a real
latency/manipulation surface at expiry. Flagged, not solved.

Uniqueness: at most one recurring series per `(seriesType, underlying, period)`. **[docs]**

### Live mainnet snapshot — 2026-07-30

**Ids roll daily; this is a shape reference, not a lookup table.** Always read
`outcomeMeta` for current ids. **[verified]**

```
961  priceBinary BTC   exp 20260731-0600  target 64009    period 1d
962  priceBinary ETH   exp 20260731-0600  target 1905.4   period 1d
963  priceBinary SOL   exp 20260731-0600  target 73.672   period 1d
964  priceBinary HYPE  exp 20260731-0600  target 53.777   period 1d
965  Recurring Fallback ("other")
966/967/968  Named outcomes index:0/1/2 — the three BTC price buckets
question 160  priceBucket BTC exp 20260731-0600 thresholds 62728,65289 period 1d
              fallbackOutcome 965, namedOutcomes [966,967,968]
```

**Quote token is USDC on mainnet.** Press coverage claimed USDH — that is
wrong for mainnet recurring markets. Testnet carries a mix of USDH and USDC,
so never assume the quote token; read `quoteToken` off `outcomeMeta`. **[verified]**

**Liquidity is real, not placeholder.** The BTC daily binary shows 50–1800
share levels roughly a tick apart, thousands of shares of aggregate depth, and
1m candles running 2000+ shares across 20+ prints per minute. **[verified]**

---

## 7. Endpoints

All **[verified]** on mainnet:

| Purpose | Call |
|---|---|
| universe | `{"type":"outcomeMeta"}` — **no `dex` argument**; outcomes aren't dex-namespaced |
| settled result | `{"type":"settledOutcome","outcome":951}` → `settleFraction` + `details` |
| book | `{"type":"l2Book","coin":"#9610"}` |
| prints | `{"type":"recentTrades","coin":"#9610"}` |
| candles | `{"type":"candleSnapshot","req":{"coin":"#9610",…}}` |
| mids | `{"type":"allMids"}` — outcomes mixed in globally, sieve on `#` |
| balances | `{"type":"spotClearinghouseState","user":…}` → `+9610` rows |
| orders | `{"type":"openOrders","user":…}` → `#9610` rows |

Websocket **[docs]**: `l2Book` / `trades` take the `#` coin and **no `dex`
field**. `outcomeMetaUpdates` streams `outcomeCreated` / `outcomeSettled` /
`questionUpdated` / `questionSettled` — this is how you detect the daily roll
without polling.

---

## 8. SDK integration — the trap

**`hyperliquid-python-sdk` 0.23.0 has zero HIP-4 support.** **[verified]**

- `Info.__init__` builds `coin_to_asset` from `spotMeta` + perp metas only.
  Outcome tokens and pairs **do not appear in `spotMeta` at all** (0 `+` tokens
  and 0 `#` universe entries out of 479 / 319). Every outcome symbol is a
  `KeyError`.
- No split/merge/negate action exists anywhere in `exchange.py`.

Our fix is `_seed_outcome_assets()`: pull `outcomeMeta` and register
`coin_to_asset`, `name_to_coin`, `asset_to_sz_decimals` for both sides of every
live outcome. After that the stock SDK order path works unmodified.

### The bug that cost us a live failure

`Exchange.__init__` constructs its **own** `Info` internally:
```python
self.info = Info(base_url, True, meta, spot_meta, perp_dexs, timeout)
```
So the `info` returned by `example_utils.setup()` is a **different object** from
`exchange.info`. Orders and cancels resolve assets through `exchange.info`.

Seeding only the returned `info` looks like it works — **split, merge, negate
and mergeQuestion all still succeed**, because those are raw actions keyed by
outcome id and never touch the asset map. Then every single order dies with
`KeyError: '#102180'`. We hit exactly this. `_setup_outcome_exchange()` now
seeds both instances.

### Refresh on roll
The seed comes from live `outcomeMeta`. Yesterday's recurring ids are dead and
today's are new, so **any long-running process must re-seed after each daily
roll** or it will be trading ids that no longer exist. Subscribe
`outcomeMetaUpdates` and re-seed on `outcomeCreated`.

---

## 9. Worked example — go short without borrowing

The book on a fresh market is often bid-only. You cannot short a token you
don't hold, and there is no borrow. The split primitive is the answer:

```python
import hip4_utils as h

# 1. Mint 100 Yes + 100 No for 100 USDC. Net position is flat.
h.split_outcome(961, 100, is_testnet=False)

# 2. Sell the 100 Yes into the bid. Now you are net short Yes / long No,
#    having never borrowed anything.
h.send_order(961, 0, price=0.86, size=100, side_str="SELL", is_testnet=False)

# 3. Later, buy the Yes back and burn the pair to reclaim the collateral.
h.send_order(961, 0, price=0.80, size=100, side_str="BUY", is_testnet=False)
h.merge_outcome(961, None, is_testnet=False)
```

Cost basis: you paid 100 USDC, received 86, and bought back at 80 → 6 USDC
profit, with 100 USDC of collateral tied up in between. The collateral cost is
the real constraint on this venue, not margin.

---

## 10. Why it's shaped like this — everything is downstream of "spot"

Nearly every intricacy above is a consequence of one design decision: **an
outcome side is not modelled as a derivative position, it is literally a spot
token.** The evidence is unambiguous: **[verified]**

- the deploy action is `spotDeploy`
- selling without inventory errors `Insufficient **spot** balance`
- balances sit in `spotClearinghouseState`, next to PURR and HYPE
- min notional is 10 USDC — the spot floor
- docs: *"Outcomes share most implementation details with spot trading"*
- builder codes work "the same as normal spot trading… earn on sell orders"

Once you accept that, the rest is a chain of patches:

| Spot-token limitation | Patch | Its own side-effect |
|---|---|---|
| Token balances can't go negative — no shorting, no borrow | **`splitOutcome`** synthesizes a short | Costs 1 quote token of collateral per share |
| Two tokens per market ⇒ two books ⇒ fragmented liquidity | **Merged book** (§3) | |
| Merged book: one order arrives via two doors | **price-side-time priority** | Makes `split` worth doing — sells outrank dual buys |
| Merged book needs `p` *and* `1−p` representable | **Fixed 0.00001 tick** (§5) | Breaks the spot sig-fig intuition; we shipped that bug |
| Collateral ops must fit the spot fills schema | **Synthetic fills** at 1/N (§4) | Nets to zero, but breaks under selective `dir` filtering |
| N tokens can't express "exactly one is true" | **`negateOutcome` / `mergeQuestion`** | |

A native signed position — a perp's `szi` — needs none of this. You'd hold −20
and be done.

**This is not a Hyperliquid quirk.** Polymarket uses Gnosis's Conditional Token
Framework, backed by exactly $1 of collateral per Yes/No pair, whose primitives
map onto HIP-4's one-for-one:

```
splitOutcome  ↔ CTF splitPosition        negateOutcome ↔ NegRiskAdapter
mergeOutcome  ↔ CTF mergePositions       mergeQuestion ↔ redeem across a condition set
```

HL adopted the CTF pattern and reimplemented it natively inside HyperCore. What
*is* theirs is the merged book as a first-class matching-engine construct with
its own priority rule.

**And it's a defensible trade.** The token model gives full collateralisation
for free — you cannot go underwater, so there is no liquidation engine, no
funding, no margin tiers, no insurance fund. Outcome tokens are also
transferable, so they compose with HyperEVM and portfolio margin in a way a
bespoke position type would not. You pay collateral to express a short and
complexity to unfragment the book; you get an instrument that cannot blow up.

**The operating rule that follows:** think of this as *spot with a
synthetic-short button*, not as a derivative. Sizing is integer shares, capital
is fully locked at 1 quote token per pair, and `mergeOutcome` is the capital-
release valve — safe to call unconditionally, since `null` merges only
`min(Yes, No)` and can never touch a directional leg.

---

## 11. What's still open

1. **Nothing sent on mainnet.** Every write path — orders, fills, and all four
   collateral primitives — is verified on testnet only. Testnet and mainnet
   have different universes and different quote tokens, and the 10 USDC floor
   is a *testnet* measurement.
2. **Jurisdiction is unresolved and blocking for mainnet.** Access failed from
   three separate locations; the cause is not the prediction-market bans (two
   of the three aren't on any such list) and looks like a broad derivatives
   geofence that would apply to the existing perp book too. Needs a
   determination on where the entity may trade event contracts before any
   mainnet order. Testnet work is unaffected.
3. ~~**The ATM-strike finding rests on one roll.**~~ **Resolved 07-31** —
   confirmed on a second roll, 8/8 exact matches, and the SOL rounding tell
   (`74.2085` settle → `74.208` target) distinguishes "derived from settlement"
   from "independently sampled mark". See §6.
4. **Zero fees are explicitly temporary.** Verified zero today, including on a
   genuine crossed fill — but do not build a strategy whose edge only survives
   at 0 bps.
5. **`szDecimals = 0` is measured, not published.** Confirmed on four distinct
   markets (including a deployer-created and a USDH-quoted one), but there is
   no field for it anywhere in the info API. If any market differs, both the
   asset seeding and the price rounding break for it.
6. **The strategy question.** Does the daily digital's implied vol differ
   systematically from our surface? It is *not* purely an ATM study — the
   contract is ATM only at 06:00 and drifts into the wings within hours (§6), so
   the comparison must be strike-aware across the day.

   ⚠ **CORRECTION — this is NOT doable from history.** Earlier drafts of this
   doc said the study needed only public historical data. That is wrong.
   **Market data for settled outcomes is deleted.** **[verified]**

   ```
   candleSnapshot  #9690 (settled)  -> HTTP 500
   recentTrades    #9610 (settled)  -> HTTP 500
   settledOutcome  961              -> OK
   ```

   Only the final result survives — `settleFraction`, `details`, and the spec.
   Every candle, trade and book snapshot evaporates at settlement.

   **Consequence: you can only study markets you captured live, and every
   uncaptured day is permanently lost.** Since recurring markets roll daily,
   a capture process is the true first task — not the analysis. Capture
   `l2Book`, `trades` and `candleSnapshot` for all live outcomes on a loop, plus
   `settledOutcome` at the roll to label each observation. Check whether the
   existing HL data pipeline can absorb outcome coins (`#…`) or needs extending.
7. **Roll-window behaviour.** Capture the first N minutes of each new daily
   book. Price discovery on a fresh ATM digital is where mispricing should
   concentrate, and it's the one moment per day that repeats identically.
8. **Expiry-window behaviour.** Settlement keys off two mark ticks straddling
   06:00 UTC (§6). Record what the book does in the final minute before
   deciding whether to hold into expiry. We have also **never watched a
   settlement occur with our own orders resting** — the one lifecycle event
   still unobserved.
9. **Deployer risk, when mainnet opens to third parties.** Today every mainnet
   outcome is protocol-settled with no discretion. `hip4_utils.py` already
   warns on any market with a non-null `deployer`; see
   [`hip4_deployer_analysis.md`](hip4_deployer_analysis.md) for why that
   matters.
