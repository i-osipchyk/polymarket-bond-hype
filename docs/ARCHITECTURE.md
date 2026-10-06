# Architecture

Technical design for the forward test described in [README.md](../README.md). Code for phases 0-8 exists; the AWS stack has been validated but not applied.

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
    pricepath/<market>/<date>T<hh>.json  hourly price and raw held-side book of an open position
    resolutions/                       outcome, payout, P&L, dispute info, days overdue at resolution
    overdue/<market>/<date>.json       daily status, price, dispute signals, conservative P&L
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
- `llm`: `review(snapshot, prompt, client, ...)` with strict schema validation, retry once then `error` (a reject), one stored call record per market, prompt and scan (a retried scan reuses it). Prompts are versioned text files in `prompts/`, selected by id in the config `llm` section together with the pinned model id. `deepseek` is the thin provider adapter (`DEEPSEEK_API_KEY` from the environment). No tools in v1.
- `arms`: `route(reject, buy)` returns the arms that trade a rules-passing candidate; only an explicit buy counts, `error` is a reject. Caps and dedup stay per arm in `entry`; rejected arms get a cooldown record so the LLM is not re-called every scan. Without an LLM client, only `baseline` trades.
- `portfolio`: balance, exposure, P&L per arm and strategy, derived from events.
- `settlement`: pure `resolve(position, raw_market)` and `assess_overdue(position, raw_market, now)`. A market settles only when Gamma says `closed`, `umaResolutionStatus == "resolved"`, outcome prices are exactly 1/0 and `closedTime` parses; anything else (disputed, 50/50, malformed) stays unsettled, so the conservative view keeps it as a total loss. `track` (hourly) writes resolutions and price-path points; `overdue` (daily) writes overdue records. One failing market never blocks the others.
- `stats`: gate computations (net EV, confidence intervals, break-even margin, drawdown, vs-baseline test). Single source of truth for the verdict. Pure functions over positions, resolutions and overdue reports. `arm_stats` gives resolved-only and conservative views (overdue = total loss, ordered last for drawdown; capital cost charged pro rata by days held); EV intervals and the arm-vs-baseline test use a seeded bootstrap (vs-baseline is one-sided at `gate.confidence`, independent resampling). `verdict` evaluates the gate on the conservative view and computes only the primary comparisons (each LLM arm vs baseline). Thresholds live in the config `gate:` section.
- `reporting`: `build_report` reads stored positions, resolutions, overdue reports, candidates and llm_calls into per-arm/strategy figures (cumulative stats and gate verdict via `stats`; today's candidates = rule-passing selections, trades, resolutions; LLM error rate from the arm's prompts; latest overdue status per unresolved market). `format_report` renders the Telegram text with resolved-only and conservative views side by side and labels non-gate cuts exploratory. `check_heartbeat` alerts if the newest `candidates/` scan file is older than `health.heartbeat_max_age_minutes`. `telegram` is the thin Bot API adapter (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from the environment); `local_report` sends the report, heartbeat check or a test alert from a local run. The CloudWatch Lambda-error alarm is deployed in phase 8.
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

## Deployment (phase 8)

- `handlers.py`: one `run_*` function per job over a `Runtime` (storage, config, LLM setup, Telegram `send`, API fetchers). Tracker and overdue send a Telegram alert listing any market that failed; the report is stored once per day (a rerun re-sends but keeps the first stored copy); `run_alarm` forwards CloudWatch alarm state changes from SNS. `lambda_entry.py` holds the one-line Lambda entry points; they use the EventBridge schedule time as `now`, so a retried invocation writes the same keys.
- `runtime.build_runtime(env)`: bucket, config path and prompts dir from env; secrets are read from SSM SecureString parameters named in env (`{prefix}/deepseek-api-key`, `/telegram-bot-token`, `/telegram-chat-id`), created by hand and never in Terraform state. Missing settings fail fast.
- `Dockerfile`: one image for all functions; config and prompts are baked in, so an image pins both versions. Each function sets its own handler through `image_config.command`.
- `infra/` (Terraform): S3 bucket (versioned, encrypted, private), ECR, one least-privilege role per function (no `s3:DeleteObject`; only scanner, tracker, overdue and report may `PutObject`), EventBridge schedules (scanner and heartbeat every 15 min, tracker hourly, overdue 06:00 and report 07:00 UTC), reserved concurrency 1 on scanner and tracker, an Errors alarm per job feeding an SNS topic and the `alarm` function.
- Every function reads all three secrets because the shared runtime builds the LLM and Telegram clients, so the IAM grant is the same set of three parameters, not per-function.
- New AWS accounts often cannot reserve concurrency (the unreserved pool must stay at 10 or more). Request a limit increase or set `reserve_concurrency = false` and accept the single-writer risk until then.

Deploy order: create the SSM parameters, `terraform apply -target=aws_ecr_repository.app`, build for `linux/amd64` and push the image to that repository, then a full `terraform apply`, then the 24 h dry run before freezing config and prompt versions.

## Pulling results for local analysis

`python -m bondhype.pull --bucket <bucket>` mirrors S3 into `data/` with the same key layout, so `local_report`, `build_report` and `verdict` run on it unchanged. It is incremental and read-only on S3: keys already present locally are skipped without downloading (stored data is append-only, so they are final), and nothing is ever overwritten or deleted. Raw `books/` and `pricepath/` are skipped unless `--heavy`; `--prefix` (repeatable) narrows to specific prefixes. It uses your normal AWS credentials (`AWS_PROFILE`) and needs `s3:ListBucket` and `s3:GetObject`. `BONDHYPE_BUCKET` can replace `--bucket`.
