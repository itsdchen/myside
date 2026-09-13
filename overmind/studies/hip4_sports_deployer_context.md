# HIP-4 sports market deployment — context for a future session

**Read this first if you are picking up the HIP-4 sports work.**

Written 2026-07-31. This is a handoff/orientation doc, not a spec.

---

## 1. What we are actually trying to do

**Goal: deploy HIP-4 outcome markets in the sports realm on Hyperliquid.**

This is a shift. Everything built before this doc was written approaches HIP-4
from the **taker** side — trading markets someone else created. We are now the
**deployer**: we create markets, and we settle them. The risk moves from
"will I be picked off" to "am I the counterparty-equivalent for everyone
else's positions."

David is starting to write code on the deployer side.

---

## 2. State of the world — read these in order

| Doc | Covers |
|---|---|
| [`hip4_guide.md`](hip4_guide.md) | Full taker mechanics. Encoding, merged book, collateral primitives, order rules, SDK integration. All measured. |
| [`hip4_deployer_analysis.md`](hip4_deployer_analysis.md) | **The important one for this work.** Deployer actions, templates, staking, settlement authority, and the void problem. |
| [`hip4_market_maker_guide.md`](hip4_market_maker_guide.md) | Structural differences vs perp/spot trading. Useful for reasoning about who will quote our markets. |
| `overmind/strat_main/tools/hip4_utils.py` | Working taker tooling. All read paths hit mainnet; all write paths verified on testnet. **No deployer actions implemented yet.** |

Nothing has ever been sent on mainnet. All write verification is testnet.

---

## 3. The blocking constraint: there is no sports template

**You cannot deploy an arbitrary market.** Validators vote on *templates*;
deployers instantiate them by filling typed blanks. **[docs]**

As of 2026-07-31, `{"type":"outcomeTemplates"}` returns exactly **one**
template on testnet, and **errors entirely on mainnet**: **[verified]**

```json
{"id": "binaryPrice",
 "role": {"standaloneOutcome": {"sideNames": ["Yes","No"]}},
 "name": "{perp} above {threshold} at {time}?",
 "keywords": [["perp","hlPerp"],["threshold","string"],["time","dateTime"]]}
```

Two consequences that shape the whole project:

1. **A sports template does not exist and we cannot create one ourselves.**
   It requires a validator vote. Getting one proposed and approved is a
   prerequisite, not an implementation detail. This is the long pole.
2. **Permissionless deployment is testnet-only right now.** Mainnet
   `outcomeMeta` has no `deployer` key at all; every mainnet outcome is
   protocol-deployed. **[verified]**

The existing hint types (`dateTime`, `date`, `string`, `hlPerp`) are
*sufficient* to express a sports template — `{home}`, `{away}`, `{date}` are
all `string`/`date`. So the constraint is governance, not expressiveness.

---

## 4. Reference implementation — study these before designing anything

Testnet already carries a **full World Cup suite**, and it is the single most
useful artifact available. All are `deployer: None`, i.e. **protocol-seeded**,
so treat them as Hyperliquid's own reference for how sports should be
structured. **[verified]**

- **~20 match markets**, questions 844–874: `World Cup: {A} vs {B}`, each a
  3-way question — `{A}` / `Draw` / `{B}` — plus an auto-created fallback.
- **One championship market**, question 823: `2026 World Cup champion`, 15
  named outcomes live, 34 already settled, plus fallback.

### The void pattern they chose — this is the key design decision

Read the resolution text of question 844 carefully. Sports have a rich taxonomy
of non-resolution (postponement, abandonment, forfeit, walkover, cancellation),
and HIP-4 has **no void primitive** — settlement pays `f` and `1−f`, and cannot
refund anyone at their entry price (see §5 of the deployer analysis for why
that is a funding constraint, not a bookkeeping one).

Their solution for **match markets**: fold cancellation into the `Draw` leg.

> *"If none of Saudi Arabia, Draw, or Uruguay has otherwise resolved to Yes,
> **Draw resolves to Yes** if: (a) the Game is canceled entirely with no make-up
> Game, or (b) the Game has not been completed by July 19, 2026 at 23:59 UTC."*

So the fallback outcome **never pays** on a match market — it stays pure
protocol plumbing, and an existing named leg absorbs the void.

Their solution for the **championship**: the opposite. Question 823 says *"If no
listed outcome is officially declared champion by that deadline, no outcome
resolves to Yes under this spec"* — which lets the **fallback** take it.

**Be deliberate about which pattern you copy.** Folding void into `Draw` has a
real economic consequence: a cancelled match hands a windfall to anyone holding
Draw and wipes out 100% of both team legs. That is *not* sportsbook void
semantics, where stakes are refunded. Anyone arriving from a sportsbook mental
model will be surprised, and "surprised" here means "lost their whole premium."

### Other conventions worth copying

From the same text, all things we would otherwise have to invent:

- **A hard backstop deadline.** Every market names an absolute date by which
  it resolves regardless (`completed by July 19, 2026 at 23:59 UTC`). Without
  this a market can hang forever — and note the deployer docs specify no
  timeout or forced-settlement path.
- **Scope of play stated explicitly**: *"results determined only after the first
  90 minutes of regular play plus stoppage time"* — excludes extra time.
- **Administrative outcomes honored**: walkover, forfeit, disqualification, or
  administrative decision all resolve to the declared winner.
- **Finality clause**: *"Once resolved, subsequent appeals, corrections,
  reversals, or result reassignments ... will not affect the market
  resolution."* Essential — settlement is irreversible on-chain.
- **Source hierarchy**: FIFA primary, *"independent reputable news sources ...
  as fallback"*.
- **Trademark disclaimer**: *"This market has not been endorsed by FIFA.
  References to 'FIFA' ... are descriptive only."*
- **A metadata convention** appended to the description:
  `metadata=category:sports|subCategory:football`

---

## 5. Operational constraints that bite hard for sports

### Rate limits will be the first wall

Testnet limits: **10 active outcomes per deployer**, 50 deploys/day. And a
question with N named outcomes registers **N+1** outcomes — the fallback counts
toward the cap. **[docs]**

A 3-way match = 3 named + 1 fallback = **4 outcomes**. So:

> **10 active outcomes ≈ 2 concurrent matches.**

For any real sports operation that is nothing — a single weekend of football is
10+ fixtures. Settling frees capacity, but matches finish on their own schedule.
Mainnet limits are unpublished. **Establish the real mainnet cap early; if it
mirrors testnet, the whole business model has to be rethought.**

### Settlement is an ongoing operational burden

Unlike the protocol's crypto binaries (deterministic, settled against a mark),
sports settlement is **manual and event-driven**:

- Games end at unpredictable times; someone/something must call
  `settleOutcome` or `settleQuestion2` promptly.
- `nameAndDescription` and `sideNames` must **exactly match** the deployed
  outcome — a checksum against settling the wrong market.
- `details` **must be empty** for deployer settlements, even though the protocol
  populates it for its own (`price:64040`). So our settlements carry *less*
  audit trail than the protocol's. Log our own reasoning off-chain.
- Question settlement is sequential: named outcomes settle to `0` in any order,
  and one settles to `1` only once it is the last active named outcome.
  `settleQuestion2` does it in a single action instead.

This implies a results-ingestion pipeline with a source hierarchy matching the
resolution text, plus alerting when a game finishes and settlement hasn't fired.

### Staking and lockup

100 HYPE on testnet; **mainnet figure unpublished** (press reported 1,000,000
HYPE, unreconciled — see deployer analysis §3). Requirements *stack* with other
deployer roles. Minimum staking duration is **183 days**, restarting on
re-activation, and you cannot deactivate while any outcome is active.

Trap: listing a season-long market (championship, futures) extends your own
lockup to that market's resolution date.

**No deployer revenue model is documented anywhere.** HIP-3 deployers earn dex
fees; the HIP-4 deployer page says nothing. Either there's an unpublished fee
share or the intended economics are monetising order flow. Worth asking
Hyperliquid directly.

---

## 6. Regulatory — do not treat this as background

Sports event contracts are the single most contested category in this space
right now, and **the deployer carries that risk directly**, not Hyperliquid.

- Brazil blocked Polymarket and 26 other prediction sites in April 2026 after
  licensed sportsbook lobbying.
- The *Prediction Markets Are Gambling Act* and *STOP Corrupt Bets Act* (both
  US, March 2026) specifically target sports contracts.
- CFTC opened rulemaking on event contracts in 2026; Kalshi's sports contracts
  are under active dispute, and in July 2026 the CFTC stayed a Kalshi emergency
  rule and ordered it to honour trades it wanted to unwind.
- Dutch Ksa banned Polymarket under gambling law and warned similar platforms.

Separately, there is an **unresolved access problem**: Hyperliquid appears
geofenced from at least three locations David tried (Mexico, Netherlands,
Japan). The pattern does not match prediction-market bans — Japan and Mexico
aren't on those lists — and looks like a broad derivatives geofence that would
affect the existing perp book too.

**Before meaningful code investment, get a determination from counsel on whether
the entity may deploy and settle sports event contracts from where it actually
operates.** This is not a blocker on prototyping against testnet, which has no
real money and is fine to proceed with.

---

## 7. Environment and how to run things

```bash
PY=/home/david/.venvs/v1/bin/python
cd /home/david/tradefi/retraded_1/overmind/strat_main/tools

$PY hip4_utils.py --mode meta --testnet --show_questions   # universe + questions
$PY hip4_utils.py --mode l2 --sym '#102180' --testnet      # a book
$PY hip4_utils.py --mode balances --testnet                # our shares
```

- Creds: `~/.creds/.Hyperliquid.creds.json`, loaded at import.
- Account `0x2AD7672c2990107b8Fd8130D896D5Bdd2aA4a6b9`; ~44.5k testnet USDC.
- `hyperliquid-python-sdk` 0.23.0 — **no HIP-4 support at all.** We seed the
  asset map ourselves and hand-roll `userOutcome` actions signed via the SDK's
  `sign_l1_action` / `_post_action`. Same approach will work for `spotDeploy`.
- Gotcha that already cost a live failure: `Exchange.__init__` builds its **own**
  `Info`, distinct from the one `example_utils.setup()` returns. Seed both.

Deployer actions are all `spotDeploy` with an `outcome` field:
`registerStandaloneOutcomeFromTemplate`, `registerQuestionFromTemplate`,
`settleOutcome`, `settleQuestion2`. Wire formats are in the deployer analysis.
**None are implemented in `hip4_utils.py` yet** — that is the code to write.

---

## 8. Suggested order of work

1. **Read question 844's full resolution text** off testnet `outcomeMeta`. It is
   the reference for everything in §4 and takes two minutes.
2. **Implement `activateOutcomeDeployer`** on testnet and confirm the 100 HYPE
   staking requirement behaves as documented. Cheapest possible real test of the
   deployer path.
3. **Implement `registerQuestionFromTemplate`** against the one existing
   template (`binaryPrice`) just to exercise the wire format end-to-end, even
   though it is not a sports template.
4. **Establish the real mainnet limits** — active-outcome cap, deploys/day,
   staking requirement. If the cap is ~10, that reshapes the plan (§5).
5. **Design the sports template(s)** and find out how templates get proposed to
   validators. This is the long pole and should start in parallel.
6. **Design the void policy explicitly** (§4) and write it into the template
   text. Decide consciously whether to follow the fold-into-Draw pattern or use
   the fallback, and document why.
7. Results-ingestion pipeline with the source hierarchy the resolution text
   promises.

---

## 9. Open questions to put to Hyperliquid directly

No amount of probing answers these:

1. How are outcome templates proposed to validators — who can propose, what
   cadence, what approval threshold? Is there a sports template planned?
2. What is the mainnet active-outcome cap, deploys/day, and staking requirement?
3. Is the deployer stake **slashable for bad settlement**, or only a capacity
   bond? Press says slashable; the docs never connect staking to settlement.
4. Is there any dispute, challenge window, or appeal against a deployer's
   settlement? The docs describe none.
5. What happens if a deployer never settles? No timeout or forced-settlement
   path is documented. What if the deployer key is lost?
6. Is there a deployer revenue model?
7. Why must `details` be empty for deployer settlements when protocol
   settlements populate it?
