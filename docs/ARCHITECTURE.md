# Architecture

Technical design for the forward test described in [README.md](../README.md). Nothing here is implemented yet.

## Stack

- Python, containerized AWS Lambda functions in **eu-west-1 (Ireland)**. Polymarket public API access from this region has been confirmed.
- Polymarket **Gamma API** for market discovery and metadata, **CLOB API** for order books and prices, data API for trades. All public and read-only; no wallet is needed for paper trading.
- **S3** for all storage. Append-only Parquet and JSON, no database. Analysis with Athena or DuckDB.
- **EventBridge** schedules. **SNS or direct calls** to a **Telegram bot** for alerts and reports.
- Config in a single YAML file, versioned. Every record is tagged with the config version that produced it.

## Jobs

| Job | Cadence | Concurrency | Does |
|---|---|---|---|
| `scanner` | every 15 min | reserved = 1 | list markets, apply rule filters, snapshot books for candidates, call LLM prompts, open paper positions per arm |
| `tracker` | hourly | reserved = 1 | snapshot open positions (price path, book), detect resolutions, compute settlements |
| `overdue` | daily | 1 | analyse positions unresolved past end date (status, price, dispute signals) |
| `report` | daily | 1 | per-arm summary and gate status to Telegram, written to S3 |
| `heartbeat` | every 15 min | 1 | dead man's switch: alert if no scan output in the last 45 minutes |

Also a CloudWatch alarm on Lambda errors, routed to Telegram.

## Data flow

```
Gamma API -> rule filters -> candidates
                                |
                    CLOB book snapshot (raw, stored)
                                |
            LLM prompts (reject, buy) -> verdicts (stored with full I/O)
                                |
        arm routing: baseline / prompt_reject / prompt_buy / mix_and / mix_or
                                |
            paper fill (walk stored book, fee from category rate)
                                |
                   positions -> tracker -> resolutions -> report
```

## S3 layout

Immutable files, partitioned by date, with deterministic keys so a retried Lambda overwrites its own output instead of duplicating it.

```
s3://<bucket>/
  markets/date=YYYY-MM-DD/...          market metadata snapshots
  candidates/date=.../                 every market evaluated, raw filter values, pass/fail + reason
  books/date=.../                      raw order book snapshots (the replayable input)
  llm_calls/date=.../                  prompt version, model id, full input, full output, tool calls
  arms/<arm>/<strategy>/
    positions/                         paper fills (avg price, slippage, fee, gross edge)
    pricepath/                         open-position price and book snapshots
    resolutions/                       outcome, dispute info, days overdue
  reports/date=.../                    daily per-arm summaries
  state/                             small dedup and cooldown JSON files per day
  config/                            config versions
```

Arms: `baseline`, `prompt_reject`, `prompt_buy`, `mix_and`, `mix_or`. Strategies: `bond`, `hype`.

Open-position state is derived from the event files. Dedup key: `arm + strategy + market_id`.

## Modules (planned)

- `markets`: Gamma client, market model, category and fee rate lookup.
- `filters`: pure functions from market + book to pass/fail with raw values. Thresholds from config.
- `books`: CLOB client, snapshot storage, book-walk fill simulation (pure and unit-testable against stored books).
- `llm`: provider adapter (DeepSeek first), tool interface (no tools in v1), strict JSON schema validation, retry-once-then-error.
- `arms`: routing rules for the five arms, caps (5 per event, 30% deployed), cooldowns.
- `portfolio`: balance, exposure, P&L per arm and strategy, derived from events.
- `settlement`: resolution detection, overdue handling.
- `stats`: gate computations (net EV, confidence intervals, break-even margin, drawdown, vs-baseline test). Single source of truth for the verdict.
- `reporting`: Telegram bot, daily report, health alerts.
- `storage`: S3 read and write helpers, deterministic keys, Parquet schemas.

## Key design decisions

- **Replayability:** the LLM sees stored snapshots only, and raw books are stored, so prompts and fill assumptions can be re-run on old candidates.
- **Honest fills:** taker-only, book-walked, fee from the market's category rate at entry. Slippage, fee and gross edge are separate fields.
- **Paired design:** every candidate that passes the rules goes to both prompts, and every arm sees the same data, so arms are directly comparable.
- **No silent defaults:** LLM errors are treated as rejects. Missing data fails closed.
- **Conservative accounting:** overdue positions count as total losses in the gate view.
- **Single writer:** reserved concurrency of 1 on scanner and tracker avoids races, since S3 has no transactions.
- **Pinned everything:** model id, prompt versions, config version are pinned and recorded per record. The model is not changed mid-test.

## Open items

- Exact DeepSeek Flash model id and API terms to be pinned at build time.
- Prompt wording for the two prompts and the fixed risk-flag vocabulary.
- Exact hard-volume floor and annualised-yield threshold for bond.
- Telegram chat and bot setup.
