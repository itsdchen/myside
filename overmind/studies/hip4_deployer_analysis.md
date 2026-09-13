# HIP-4 deployer actions — analysis, and the settlement-risk question

Written 2026-07-31. Source: [HIP-4 deployer actions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-4-deployer-actions)
(note: this page is **not listed in `llms.txt`** — it's unindexed, easy to miss).
Companion to `hip4_guide.md`, which covers the taker side.

Status tags as elsewhere: **[verified]** = probed live. **[docs]** = from the
page. **[assumed]** = our inference.

---

## 1. What this page actually governs

Everything in `hip4_guide.md` is the *taker* side — trading markets someone
else created. This page is the *deployer* side: how markets come into
existence and, more importantly, **how they get settled**.

That second part is why this matters even if we never deploy anything. The
deployer is the entity that decides what our position pays out. Reading this
page is due diligence on counterparty-equivalent risk, not a build spec.

### Live status **[verified]**

| | mainnet | testnet |
|---|---|---|
| `outcomeTemplates` | **errors** — `Failed to deserialize the JSON body` | works, returns 1 template |
| `outcomeMeta` → `deployer` field | **key absent entirely** | present; 14 of 130 outcomes deployer-created |

**Permissionless HIP-4 deployment is testnet-only right now.** Mainnet runs
protocol-deployed recurring markets exclusively, and the request type isn't
even recognised there. So today every mainnet outcome is settled by the
protocol against a mark price — deterministic, no human discretion. That
changes the moment third-party deployers go live on mainnet, and it changes
what we're exposed to.

The single existing template is `binaryPrice`:
*"{perp} above {threshold} at {time}?"*

---

## 2. The template system

Deployers cannot write free-form markets. **Validators vote on templates**;
deployers instantiate them. A template fixes display name, description, side
names, and a set of typed `{keyword}` placeholders. **[docs]**

Three roles: **standalone outcome** (one Yes/No market), **question** (the
container), **question outcome** (one named outcome, which must declare its
parent question template — instantiations are only accepted under that parent).

Keyword hint types:

| Hint | Format |
|---|---|
| `dateTime` | `%Y%m%d-%H%M`, within the next year |
| `date` | `YYYYMMDD` (end of day), within the next year |
| `string` | free text |
| `hlPerp` | coin name of an existing perp, incl. HIP-3 form `test:ABC` |

Values ≤100 chars, no `{`, `}`, or `|` (`:` allowed, for HIP-3 coin names).

Derived on-chain identity is mechanical: name becomes `template:<id>`,
description becomes keyword-value pairs sorted and joined —
`expiry:20260801-0600|target:100|underlying:BTC`. The `template:` prefix is
reserved and only template deployments can produce it.

**Why this design.** The template layer is doing the job that Polymarket's
resolution-criteria text and Kalshi's filed rulebook do: pinning down what the
market means *before* anyone can trade it. By forcing every market through a
validator-approved skeleton and reducing the deployer's freedom to filling in
typed blanks, HIP-4 removes the single largest source of prediction-market
disputes — ambiguous wording. That's a genuinely good design decision, and it's
strictly more constrained than Polymarket, where a deployer writes prose.

But note what it does *not* constrain: the template fixes the **question**, not
the **answer**. Nothing in the template system governs how the deployer decides
what actually happened.

### Composition with HIP-3
The `hlPerp` hint resolves HIP-3 builder perps, and testnet already has live
deployments against them — `perp:xyz:XYZ100` and `perp:magk:7AG5Z`. **[verified]**
`xyz:XYZ100` is the same symbol our `hl_utils.py` defaults to. So an outcome
market can be struck on a builder-deployed perp: HIP-3 and HIP-4 compose
directly. If we ever run our own HIP-3 dex, we could deploy digitals on our own
perps.

---

## 3. Deployer economics

| Item | Value | Note |
|---|---|---|
| Staking requirement | **100 HYPE (testnet)** | mainnet figure not published **[docs]** |
| Deploy gas cost | **zero** | capacity is rate-limited instead |
| Active outcomes cap | 10 (testnet) | settling frees capacity |
| Deploys per day | 50 (testnet) | |
| Min staking duration | **183 days**, restarts on re-activation | |
| Deactivation | requires duration elapsed **and** no active outcomes | |
| Account type | Standard account abstraction required | |

Staking requirements **stack** with the deployer's others (HIP-3 DEX, quote
token), so a firm running both pays both.

Press coverage claimed **1,000,000 HYPE** for mainnet HIP-4 deployment against
the docs' 100 HYPE testnet figure. I can't reconcile those from official
sources because the mainnet path isn't live to check. If the 1M number is real
that's ~$30M+ at current HYPE — a serious capital commitment that only makes
sense against a revenue model.

**And the revenue model is the conspicuous gap.** HIP-3 deployers earn a share
of fees on their dex. This page says *nothing* about deployer revenue. HIP-4
fees are currently zero anyway, and builder codes (which do work on outcomes)
are a separate mechanism available to anyone routing order flow, not to the
deployer specifically. So as documented: 183-day capital lockup, no stated
revenue. Either there's an unpublished fee share, or the intended deployers are
firms monetising the order flow rather than the venue.

The 183-day lockup combined with "no active outcomes" is worth flagging as an
operational trap: a deployer who lists a market dated 11 months out has
effectively extended their own lockup to that date, because they cannot
deactivate while it's live.

---

## 4. Settlement authority — where the risk lives

Four actions: `registerStandaloneOutcomeFromTemplate`,
`registerQuestionFromTemplate`, `settleOutcome`, `settleQuestion2` (the
original `settleQuestion` is discontinued).

The settlement rules that matter:

- **Standalone outcomes may settle to any fraction in [0, 1]** — e.g. `"0.66"`.
  This is the "bounded options-like instruments" capability from the HIP-4
  pitch: scalar, not binary, payouts.
- **Question outcomes must settle to exactly `"0"` or `"1"`.**
- Question settlement is sequential: named outcomes settle to `0` in any order;
  one settles to `1` once it is the last active named outcome, which
  auto-settles the fallback to 0 and closes the question.
- `settleQuestion2` does it in one shot: must cover exactly the remaining
  active named outcomes, exactly one at `1`, rest at `0`, fallback auto-0.
- `nameAndDescription` and `sideNames` must exactly match the outcome — a
  checksum against settling the wrong market.
- The named outcome set is **fixed at creation** (max 100). Adding outcomes to
  a live question is a future upgrade.

### An asymmetry worth noting
For deployer settlements, **`details` must be empty**. But protocol-settled
recurring markets *do* populate it — we read `"details": "price:64040"` off
mainnet outcome 951. **[verified]**

So deployer-settled markets carry **less on-chain audit trail than
protocol-settled ones**. There is no field in which a deployer records *why*
they settled the way they did. For a discretionary settlement that's exactly
the field you'd want.

---

## 5. The void problem

This is the question the page doesn't answer, and it's the right thing to have
asked. A game gets cancelled. An election is postponed. The data source stops
publishing. What happens to open positions?

### What HIP-4 provides

**For questions: the fallback outcome.** Every question auto-creates a fallback
(named `template fallback`, description `other`). If none of the named outcomes
occur, the fallback is what settles to 1. We've seen this live — mainnet
outcome 965 is "Recurring Fallback" with description `other`, and settled
outcome 957 was a fallback that settled to `0.0`. **[verified]**

But read what that actually does. Game cancelled → fallback pays 1 → **every
named outcome settles to 0**. So everyone who bought "Team A wins" loses their
full premium, and everyone who sold it keeps the whole thing. That is not a
void. It is a substantive resolution of "no listed outcome occurred," and it
transfers wealth from Yes-holders to No-holders on every named leg.

**For standalone outcomes: nothing.** There is no fallback. The only lever is
`settleFraction`, and the natural pseudo-void is `"0.5"` — pay everyone half.

### Why there is no true void, and cannot be

A true void returns each trader their own entry price. **No order-book
prediction market can do this, HIP-4 included — and the reason is funding, not
bookkeeping.**

The naive objection is that a token balance is the net residue of many trades
at many prices, so there's no single "stake" to refund. True, but a centralised
venue with full position accounting *could* track each trader's basis. Kalshi
could compute it tomorrow. So bookkeeping isn't the real constraint.

The real constraint is that **basis-refund doesn't balance**. Collateral in an
outcome market is exactly **1 quote token per Yes/No pair outstanding** — that
is what `splitOutcome` locked up and what `mergeOutcome` releases. So any
settlement must pay the Yes holder `f` and the No holder `1 − f` for some
single fraction `f`. That is the only self-funding form.

Now take a matched pair where the Yes holder bought at 0.90 and the No holder
bought at 0.30. Refunding both at basis pays out 1.20 against 1.00 of
collateral. The 0.20 has to come from somewhere, and it can't come from the
traders who already exited with profit — they've withdrawn. **The pot is short.**

This is why every venue converges on picking a fraction:

- Kalshi's last-fair-price: pays `f` and `1 − f` → **balances**
- Polymarket's 50-50: pays `0.5` and `0.5` → **balances**
- Sportsbook stake refund: **balances only because there is no secondary
  market** — a bet is bilateral, never transferred, so the "pair" is always the
  original two parties at complementary prices

The sportsbook can refund stake precisely because it never let the position
change hands. The moment positions become fungible and transferable, the
complementarity between the two sides' bases is destroyed, and stake-refund
stops being fundable. Every design choice below follows from that constraint.

**This is the structural difference from a sportsbook.** A sports bet is a
bilateral contract with the book at a fixed stake, never transferred to anyone.
Refunding the stake is trivially well-defined. The moment positions become
**fungible and transferable**, refund-at-stake dies as a concept. Every venue
that runs a real order book has had to invent a substitute.

### How the comparables actually handle it

| Venue | Void mechanism | What a holder gets | Wealth transfer? |
|---|---|---|---|
| **Sportsbook** | Void / push — game postponed beyond ~24–48h (varies by book) or abandoned → bets void | **Exact stake back** | None — it's as if the bet never happened |
| **Polymarket** | UMA DVM can resolve **50-50**; many markets carry explicit "if X does not occur by [date], resolves 50-50" language | **$0.50/share regardless of entry** | **Large.** A market trading 0.90 that voids to 0.50 moves 0.40/share from Yes to No |
| **Kalshi** | Void at **"last traded fair price"** — exchange estimate of fair value immediately before the disqualifying event, from recent prints and resting depth over a defined window | **The mark just before the break** | **Minimal** — everyone is marked out at the last honest price |
| **HIP-4 question** | Fallback outcome settles to 1 | Named-outcome Yes holders get **0** | **Total** on every named leg |
| **HIP-4 standalone** | Deployer picks any `settleFraction` ∈ [0,1] | **Whatever the deployer chooses** | **Deployer's discretion** |

Kalshi's approach is clearly the best-designed of the four. It doesn't refund
your entry — nothing can — but it ensures the *void itself* doesn't move money
between traders. You're marked out at what the position was worth the moment
the market broke. Polymarket's 50-50 is crude by comparison and is openly
acknowledged to hand strong-conviction holders back half their stake "regardless
of what they believed was obvious."

### Where HIP-4 lands

Note that HIP-4's standalone `settleFraction` is **capable** of the Kalshi
approach: a conscientious deployer facing a cancelled event could settle at the
pre-event mark and mark everyone out fairly. The primitive supports it.

**Nothing requires it.** And that's the finding:

> HIP-4 gives the deployer **strictly more settlement discretion** than either
> comparable, with **strictly less documented recourse**.

- Kalshi is a CFTC-regulated DCM with a filed rulebook, and even so its
  settlement authority is contested — in March 2026 it refunded $2.2M over a
  "death carveout" provision, and in July 2026 the **CFTC stayed a Kalshi
  emergency rule and ordered the exchange to honour trades it had proposed to
  unwind**. There is an appeals path, and it has been used.
- Polymarket has UMA disputes escalating to a token-holder vote. Contentious
  and gameable by large holders, but it is a process.
- **HIP-4, as documented, has neither.** The deployer calls `settleOutcome`
  with a fraction of their choosing. This page documents no dispute mechanism,
  no challenge window, no appeal, and no slashing condition. Press coverage
  mentions a slashable stake; the docs describe staking as a *capacity*
  requirement and never connect it to settlement misbehaviour.

For a trader, that is the exposure to underwrite. On mainnet today it's
moot — protocol settlement against a mark price, no discretion. It stops being
moot the day third-party deployers go live.

---

## 6. Consolidated open questions

### Settlement integrity — the ones that matter for taking risk

1. **Is there any dispute or challenge mechanism?** Nothing documented. If a
   deployer settles a binary wrongly, what is the recourse?
2. **Is the deployer stake actually slashable for bad settlement**, or only a
   capacity bond? Press says slashable; docs say neither way.
3. **What if the deployer never settles at all?** No deadline, timeout, or
   forced-settlement path is documented. Can a market hang indefinitely with
   collateral locked? What if the deployer's key is lost?
4. **Can a deployer settle early**, before the stated expiry? No timing
   constraint appears anywhere.
5. **Who may settle besides the deployer?** Is there a protocol backstop?
6. **Why must `details` be empty** for deployer settlements when protocol
   settlements populate it? This removes the natural audit field.

### Void specifically

7. Is `settleFraction = 0.5` the intended void convention for standalone
   outcomes, or is there guidance we haven't found?
8. For questions, is "all named outcomes → 0, fallback → 1" the sanctioned
   cancellation path? The docs describe the mechanics but never name it as the
   void handler.
9. Are deployers expected to encode void conditions in the template
   description, the way Polymarket puts them in resolution criteria? The
   templates we've seen (`binaryPrice`) contain no void language at all.

### Economics

10. **Mainnet staking requirement** — 100 HYPE testnet vs press-reported 1M
    mainnet, unreconciled.
11. **Do deployers earn anything?** No revenue model documented against a
    183-day lockup.
12. Mainnet rate limits (10 active / 50 per day are testnet figures).
13. How do validators vote on templates — who proposes, what cadence, what
    approval threshold?

### Mechanics we could still test on testnet

14. What happens to **resting orders at settlement**? Presumably cancelled, but
    we haven't watched a settlement happen with our own orders on the book.
15. Can the **fallback outcome be traded** like any other? It appears in
    `outcomeMeta` with its own book and mids (mainnet `#9650`/`#9651` both at
    0.5), which suggests yes — and a tradeable "none of the above" is itself
    interesting.
16. Does `negateOutcome` include the fallback? **Already answered: yes**
    — verified on testnet question 820. **[verified]**

---

## 7. Bottom line

The template system is well designed and solves the ambiguity problem better
than Polymarket does. The settlement layer is where the risk concentrates, and
it is materially less specified than either comparable — more deployer
discretion, no documented dispute path, and no audit field.

None of this bites today, because mainnet HIP-4 is entirely protocol-settled
against mark prices. The practical rule that follows:

> **Trade protocol-deployed recurring markets freely. Before taking size in any
> third-party-deployed market, check `outcomeMeta` for a non-null `deployer`
> and treat that market's settlement as discretionary counterparty risk.**

That check is one field and we should build it into `hip4_utils.py` as a
warning on any market with a deployer set — cheap now, and it will be load-
bearing the day mainnet opens to third-party deployers.

---

## Sources

- [HIP-4 deployer actions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-4-deployer-actions)
- [Polymarket resolution docs](https://docs.polymarket.com/concepts/resolution) and [how markets are disputed](https://help.polymarket.com/en/articles/13364551-how-are-markets-disputed)
- [Kalshi fee refunds on disputed settlements](https://gamingamerica.com/news/1075098/kalshi-will-now-refund-trading-fees-on-disputed-settlements)
- [CFTC stays Kalshi emergency rule, orders trades honoured (July 2026)](https://www.governmentenforcementreport.com/2026/07/cftc-stays-kalshi-emergency-rule-directs-exchange-to-honor-trades-in-unprecedented-exercise-of-federal-authority/)
- [Trader account of Kalshi void rules](https://ufoholdings.substack.com/p/i-lost-30k-due-to-kalshis-void-rules)
- [FanDuel house rules](https://www.fanduel.com/fanduel-sportsbook-house-rules-oh), [why bets are voided](https://www.covers.com/industry/why-are-bets-voided)
