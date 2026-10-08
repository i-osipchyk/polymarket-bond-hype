# Implementation Plan

Build order for the design in [../README.md](../README.md) and [ARCHITECTURE.md](ARCHITECTURE.md). Each phase ends with something runnable and testable. Pure logic (filters, fills, stats) is built test-first against fixtures and stored books.

## Phase 0: Project skeleton

- Python project layout, dependency management, lint and test setup.
- YAML config schema with a version field, loaded and validated at startup.
- Local runner that executes any job against a local directory instead of S3 (a storage interface with local and S3 backends), so everything can be developed without AWS.

**Done when:** `pytest` runs, config loads and validates, storage round-trips locally.

## Phase 1: Market data and filters

- Gamma client: list active markets, metadata, category, end date, event id.
- CLOB client: order book and price history for a token.
- Fixtures: record real responses for a handful of markets (bond-like, hype-like, edge cases).
- `filters`: bond and hype rule filters as pure functions returning pass/fail plus raw values and reason. Bond priority in the 90–95¢ overlap.
- Candidate logging: every evaluated market, including rejected ones.

**Done when:** a local scan over live data produces a candidates file with raw values and reasons; filter tests pass on fixtures.

## Phase 2: Paper fills and portfolio

- Book-walk fill simulation: average price, slippage, shares, fee from the category rate (read at entry), gross edge.
- Position model and portfolio derived from event files: balance, exposure, per-event cap (5), deployed cap (30%).
- Dedup by `arm + strategy + market_id`, with a cooldown for rejected and refused markets that ends only on a price move of at least 3¢.
- Fill simulation unit tests, including thin books, partial fills and fee maths.

**Done when:** given a candidate and a stored book, the system opens a paper position with all cost fields logged, and caps and dedup hold.

## Phase 3: LLM layer

- Provider adapter (DeepSeek Flash first; confirm model ID and API terms at this point), tool interface with no tools in v1.
- Strict JSON schema validation; retry once, then `error` treated as reject.
- Fixed risk-flag vocabulary and the two prompts (reject-by-default, buy-by-default), versioned.
- Full prompt, input and output stored per call.
- Replay tool: re-run a prompt version over stored candidates.

**Done when:** both prompts return valid verdicts on a set of stored candidates, errors fail closed, and replay is reproducible.

## Phase 4: Arms

- Routing for `baseline`, `prompt_reject`, `prompt_buy`, `mix_and`, `mix_or`, each an independent portfolio per strategy.
- The scanner job end to end locally: scan, filter, snapshot, LLM, route, fill.

**Done when:** one local scan produces positions in the right arms, with the same entry price across arms for the same market.

## Phase 5: Tracking and settlement

- `tracker`: price path and periodic book snapshots for open positions.
- Resolution detection and settlement: win or loss, payout, dispute information.
- `overdue`: positions past end date, status, price and dispute signals. Conservative accounting (total loss until resolved).

**Done when:** positions settle correctly on resolved markets (checked against historical resolved markets), and overdue handling matches the README.

## Phase 6: Stats and gate

- `stats`: net EV per trade, win rate against break-even, confidence intervals, drawdown, worst loss, comparison against baseline at 95%, resolved-only and conservative views.
- Gate verdict computed in one place, with primary comparisons separated from exploratory ones.
- Tests with synthetic trade sets, including skewed payoffs and overdue cases.

**Done when:** the gate verdict is produced from stored positions and is covered by tests.

## Phase 7: Reporting and alerts

- Telegram bot: daily per-arm report (candidates, trades, resolutions, EV with interval, win rate against break-even, drawdown, LLM error rate, overdue list).
- Health alerts: Lambda error alarm, dead man's switch for missing scans.

**Done when:** a daily report and a test alert reach the Telegram chat from a local run.

## Phase 8: AWS deployment

- Container image and Lambda functions for `scanner`, `tracker`, `overdue`, `report`, `heartbeat`.
- EventBridge schedules (15 min scan, hourly tracker, daily overdue and report), reserved concurrency of 1 on scanner and tracker.
- S3 bucket and layout, IAM least privilege, secrets (LLM key, Telegram token) in Secrets Manager or SSM.
- CloudWatch alarms. Infrastructure as code (Terraform or CDK; to be chosen).
- Dry run for 24 hours before the test officially starts; check data completeness, then freeze config and prompt versions.

**Done when:** a full day of scans runs unattended with no gaps, alerts have been tested, and the first report arrives.

## Phase 9: Forward test

- Start date and frozen config version recorded in the repo.
- Weekly check of data completeness and the report, with no tuning of thresholds or prompts.
- Evaluate the gate only when each arm has the minimum 100 resolved trades per strategy.

**Done when:** the gate verdict is produced and recorded, and we decide whether to go live, extend the test or stop.

## Later (not in v1)

Live execution adapter (small capital, after the gate), risk-based sizing, stop-losses and take-profits evaluated offline from the logged price paths, passive orders, scale-in, live web-search arm, dashboard.

## Risks to watch

- **Slow sample:** about 1–2 resolved trades per day per strategy means the gate can take months, so watch fill rates early and decide up front whether to extend the test.
- **Data gaps:** missed scans reduce the sample and can bias it, hence the heartbeat and the dry run.
- **API drift:** Gamma and CLOB schemas and fee rates can change, so record raw responses and validate them on read.
- **Multiple comparisons:** the 10 portfolios make a false winner likely, which is why the primary comparisons and the 95% bar are fixed in advance.
- **Jurisdiction:** read access works from Ireland, but live trading is subject to Polymarket's terms and restrictions that apply to you personally. Check them before the live phase.
