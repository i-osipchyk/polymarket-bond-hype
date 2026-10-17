# CLAUDE.md

Project: forward test of the Polymarket "bond" and "hype" strategies. Read [README.md](README.md) for the agreed design and [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical layout; [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) has the build order. The README and architecture files are the source of truth; if code and docs disagree, resolve it deliberately and update the docs.

## Ground rules

- **Paper trading only.** Do not add code that places real orders or handles wallets or keys unless the user explicitly asks. Live trading comes only after the go-live gate in the README passes.
- **The go-live gate is fixed.** Do not change gate thresholds or verdict logic to fit results. The verdict is computed in one place (`stats`).
- **Do not change the pinned model, prompt versions or filter thresholds mid-test.** Changes go through the config file with a new version, and every record carries its config version.
- **Fail closed.** LLM errors, invalid JSON or missing data mean reject, never buy.
- **The LLM is a veto screener, not a forecaster.** Keep the output schema strict (verdict, fixed-vocabulary risk flags, confidence, reason).
- **Never delete stored data.** Trades and LLM decisions live in two state tables (`state/trades.csv`, `state/decisions.csv`) that the jobs rewrite as a whole, always with a conditional write on the version they read; the bucket is versioned, so earlier versions stay recoverable. Everything else (candidates, LLM audit with full I/O and costs, price paths, reports) is written once per run, never one file per record.
- **Do not add scope the design defers** (Kelly sizing, stops, passive orders, scale-in, web-search arm, dashboard) without asking.

## Conventions

- Python. Thresholds and parameters come from the YAML config, not constants in code.
- Filters, fill simulation and stats are pure functions so they can be unit-tested against stored books and fixtures.
- Fees are read from the market's category rate at entry time, never hard-coded.
- Keep fee, slippage and gross edge as separate logged fields.
- Arm names: `baseline`, `prompt_reject`, `prompt_buy`, `mix_and`, `mix_or`. Strategy names: `bond`, `hype`. Bond takes priority in the 90–95¢ overlap.
- Label any analysis outside the primary comparisons (each LLM arm vs baseline, per strategy) as exploratory.
- Jobs that write the trades table never run at the same time: scanner then tracker in the hourly `bondhype-cycle` state machine, overdue at 06:40.

## Facts to verify rather than assume

- Polymarket fee rates and API schemas change; check the [fees docs](https://docs.polymarket.com/trading/fees) and the live API.
- The DeepSeek Flash model id and API behaviour have not been verified; pin them at build time.
