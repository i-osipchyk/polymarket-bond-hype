# polymarket-bond-hype

Forward-tested ("paper traded") implementation of two Polymarket strategies, with an LLM veto layer on top of rule-based filters.

**Status:** design agreed, no code yet. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical design and [CLAUDE.md](CLAUDE.md) for working conventions.

## Strategies

- **Bond:** buy the side priced 90–95¢ in markets resolving in 1–14 days. Small, frequent wins; rare large losses (a 94¢ buy risks 94¢ to make 6¢).
- **Hype:** buy NO on a hyped low-probability outcome (YES priced 5–25¢, so NO at roughly 75–95¢), resolving in 1–14 days. Same payoff shape as bond, different candidate source (favourite–longshot bias).

Where the price bands overlap (90–95¢), **bond takes priority**: one strategy label per trade.

## Goal

1. Paper trade and measure whether either strategy has edge after fees and slippage.
2. If the gate below passes, move to automated live trading with small capital.

## Pipeline

1. **Rule filters** select candidates from Gamma/CLOB market data.
2. **LLM veto layer** reviews each candidate. It never forecasts outcomes; it only looks for reasons not to trade.
3. **Paper execution** simulates a taker order that walks the recorded order book.
4. **Settlement tracking** records resolution, disputes and overdue positions.
5. **Reporting** sends health alerts and a daily per-arm report to Telegram.

## Rule filters (all values are config, not constants)

| Filter | Bond | Hype |
|---|---|---|
| Side / price band | 90–95¢ | buy NO where YES is 5–25¢ |
| Time to resolution | 1–14 days | 1–14 days |
| Liquidity | best ask + next level hold ≥ 3× order size | same |
| Spread | ≤ 2¢ | ≤ 3¢ |
| Total volume | ≥ ~$2k | ≥ ~$2k |
| Other | annualised-yield check | exclude markets decided by a single insider-knowable event |

There is no hype-proxy filter in v1. Every candidate's raw values are logged, including rejected ones.

## LLM layer

- Role: **veto screener only**. Not a probability forecaster.
- Input: stored snapshots only (question, full resolution rules, end date, price history, volume, book summary, sibling markets in the event), so any run can be replayed. The call sits behind a tool interface so live web search can be added later as a separately logged arm.
- Output: strict JSON: `verdict` (`buy` | `reject`), `risk_flags` from a fixed vocabulary (`ambiguous_resolution`, `scheduled_catalyst`, `dispute_risk`, `thin_book`, `insider_risk`, `already_decided`), `confidence` (1–5), `reason`.
- Failure handling: retry once, then record `error` and treat as reject. Never default to buy.
- Model: DeepSeek Flash for the first run (exact model ID pinned in config), temperature 0, model fixed for the whole test.
- Two prompts, both run on every candidate:
  - **reject-by-default:** must justify a buy.
  - **buy-by-default:** must find a reason to reject.

## Arms

Each arm is an independent paper portfolio ($1,000 balance, own caps, own P&L), run for both strategies (10 portfolios in total). The same market may appear in several arms; entry prices match because fills are simulated from the same book snapshot.

| Arm | Trades when |
|---|---|
| `baseline/` | rules pass (no LLM) |
| `prompt_reject/` | rules pass and the reject-by-default prompt says buy |
| `prompt_buy/` | rules pass and the buy-by-default prompt says buy |
| `mix_and/` | both prompts say buy |
| `mix_or/` | at least one prompt says buy |

## Sizing and risk

- Fixed **$10 per trade**, **$1,000** paper balance per arm.
- Caps: at most 5 open positions per event, at most 30% of the bankroll deployed at once.
- One entry per market per arm, at the first scan where it passes the rules and the arm's verdict is buy. Rejected markets are re-evaluated only when the ask has moved at least 3¢ from the price at the last rejection, either way; time alone never ends the cooldown. An entry that the portfolio refuses (deployed or event cap, no liquidity, missing fee schedule) is cached the same way, and the refusal reason is logged.
- Idempotency key: arm + strategy + market id.
- Hold to resolution. No stops, no take-profits in v1. The full price path and periodic book snapshots of open positions are logged so stops and exits can be evaluated offline.

## Fills and costs

- Taker only. Each paper entry walks the recorded order book at signal time.
- Fee = `shares × feeRate × p × (1 − p)`, with the rate read from the market's category at entry time (see the [Polymarket fees docs](https://docs.polymarket.com/trading/fees)). Fee, slippage and gross edge are logged separately.
- Passive (maker) orders are not simulated; snapshots can't tell us honestly whether they would fill.

## Overdue positions

A position still unresolved after its end date counts as a **total loss** in the conservative gate view from the first day, and settles at its real result when the market resolves. The daily report shows resolved-only and conservative numbers side by side, plus days overdue. A job analyses each overdue position (status, price, dispute signals).

## Go-live gate

Written before the test starts and not to be relaxed afterwards. Evaluated per strategy and per arm:

- At least **100 resolved paper trades**.
- Net EV per trade after fees, slippage and capital cost is positive.
- The arm beats baseline at **95% confidence** (primary comparison: each LLM arm vs baseline, per strategy). The observed win rate must also beat the entry-price break-even rate by a margin (for example ~96% at a 94¢ average entry).
- Worst single loss and max drawdown stay within **10% of the paper balance**.
- An LLM arm counts as useful only if it beats baseline on net EV per trade (or loss rate at a similar number of trades). Otherwise drop the LLM and go live on rules alone.
- With 10 portfolios, only the primary comparisons count; everything else in the reports is labelled **exploratory**.
- Verdict logic lives in code and is computed identically every time.

## Out of scope for v1

Kelly or risk-based sizing, stop-losses and take-profits, scale-in entries, passive orders, live web search arm, automated dispute-risk exits, dashboard.
