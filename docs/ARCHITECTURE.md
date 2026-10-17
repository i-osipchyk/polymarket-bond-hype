# Architecture

Technical design for the forward test described in [README.md](../README.md). Code for phases 0-8 exists; the AWS stack has been validated but not applied.

## Stack

- Python, containerized AWS Lambda functions in **eu-west-1 (Ireland)**. Polymarket public API access from this region has been confirmed.
- Polymarket **Gamma API** for market discovery and metadata, **CLOB API** for order books and prices, data API for trades. All public and read-only; no wallet is needed for paper trading.
- **S3** for all storage, no database: two CSV state tables rewritten by the jobs, plus write-once JSON files. Analysis with DuckDB or pandas.
- **EventBridge** schedules and a **Step Functions** state machine for the hourly scan-then-track cycle. **SNS or direct calls** to a **Telegram bot** for alerts and reports.
- Config in a single YAML file, versioned. Every record is tagged with the config version that produced it.

## Jobs

| Job | Cadence | Concurrency | Does |
|---|---|---|---|
| `scanner` | hourly, on the hour, step 1 of `bondhype-cycle` | sequential with tracker | list markets, apply rule filters, snapshot books for candidates, call LLM prompts, open paper positions per arm |
| `tracker` | hourly, step 2 of `bondhype-cycle` | sequential with scanner | snapshot open positions (price path, book), detect resolutions, compute settlements |
| `overdue` | daily 06:40 UTC | 1 | analyse positions unresolved past end date (status, price, dispute signals) |
| `report` | daily | 1 | per-arm summary and gate status to Telegram, written to S3 |
| `heartbeat` | hourly | 1 | dead man's switch: alert if no scan output in the last 90 minutes |

Also a CloudWatch alarm on Lambda errors and on failed, timed-out or aborted cycle executions, routed to Telegram.

`bondhype-cycle` is a Step Functions state machine started by EventBridge every hour on the hour. It runs the scanner, then the tracker, so the two jobs that write the trades table every hour never overlap. Each task is retried once after 60 s. A failed scan still lets the tracker run, and the execution then fails so the alarm fires. Both tasks get the event's `time` as their run time. The execution times out after 55 minutes, before the next one starts. Overdue, the only other job that writes the trades table, runs at 06:40, after the 06:00 cycle has normally finished. If a run does overlap, the conditional write (see below) refuses the later save, and that run fails and is retried.

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

Two state tables, rewritten as a whole by the jobs that own them, and write-once files. A retried
run rereads the tables and redoes its work; the write-once files of a scan are keyed by its time,
so a retry replaces them, except the LLM audit, which keeps every attempt.

```
s3://<bucket>/
  state/trades.csv                     one row per arm, strategy and market (see below)
  state/decisions.csv                  one row per LLM-reviewed candidate per scan (see below)
  candidates/date=.../scan=<ts>.jsonl  every market evaluated, raw filter values, pass/fail + reason
  llm_calls/date=.../scan=<ts>/run=<id>.jsonl  one file per scan attempt: per call, prompt version,
                                       model id, full input, every attempt's output, token usage
                                       and cost_usd
  pricepath/date=.../run=<date>T<hh>.jsonl  one file per tracker run: per open market, its holders
                                       (arm, strategy, side), prices and raw held-side books
  reports/date=.../                    daily per-arm summaries
  config/                              config versions

  legacy, no longer written (kept for history; bondhype.migrate built the tables from them):
  arms/<arm>/<strategy>/positions/     one file per paper fill
  arms/<arm>/<strategy>/resolutions/   one file per resolution
  arms/<arm>/<strategy>/overdue/<market>/<date>.json  daily overdue analysis
  arms/<arm>/<strategy>/pricepath/<market>/<date>T<hh>.json  per-position price points
  state/cooldowns/<arm>/<strategy>/<market>/<ts>.json  LLM-arm rejections
  state/ledger/date=.../scan=<ts>.json.gz  ledger checkpoints
  llm_calls/date=.../market=<id>/<prompt>/scan=<ts>.json  one file per LLM call
```

Arms: `baseline`, `prompt_reject`, `prompt_buy`, `mix_and`, `mix_or`. Strategies: `bond`, `hype`.

**`state/trades.csv`** (`ledger`): one row per trade, keyed by `arm + strategy + market_id`, with
`status` (`open`, `overdue`, `resolved`). It holds the paper fill (avg price, best ask, slippage, fee,
gross edge, partial, config version), the resolution once there is one (outcome, payout, P&L,
dispute statuses, days overdue at resolution) and the latest overdue analysis (`overdue_checked_on`,
days overdue, UMA status, held-side price, conservative P&L). The scanner adds rows, the tracker
resolves them and the overdue job annotates them.

**`state/decisions.csv`** (`decisions`): one row per scan, strategy and market sent to the LLM: side,
ask, question, model, config version, each prompt's verdict, risk flags, confidence, reason and
`cost_usd` (`buy_*`, `reject_*`; the reject prompt is `skipped` when the buy prompt did not say
buy), and one column per LLM arm with what it did: `opened`, `rejected`, `refused:<reason>`,
`held` or `cooldown`. Cooldowns are derived from it (see `arms` below). Candidates that no LLM arm
could trade get no row.

Both tables are plain CSV with a header row; floats are written with `repr`, so they read back
exactly, and lists (risk flags, UMA statuses) are JSON arrays. The bucket is versioned, so every
earlier version of a table stays recoverable. Nothing ever deletes an object.

### Request cost

Every S3 request is billed, and request charges, not storage, are the S3 cost. Each job reads the
tables it needs once and writes each at most once:

| Job | Reads | Writes |
|---|---|---|
| scanner | trades, decisions | trades, decisions, one LLM audit file, one candidates file |
| tracker | trades | trades (only if something settled), one price-path file |
| overdue | trades | trades |
| report | trades, decisions, today's candidates files | the report (once a day) |
| heartbeat | lists today's and yesterday's `candidates/` | nothing |

- **Conditional writes.** A table is read with its ETag and written with `If-Match` on it, or
  `If-None-Match: *` when it does not exist yet (`Storage.read_versioned` / `write_versioned`). A
  job that would overwrite another job's changes fails with `StaleWrite` instead, and its retry
  rereads the table.
- **Fail closed on a missing table.** If `state/trades.csv` is missing but legacy position files
  exist, `load_ledger` raises `MissingTradesTable` rather than starting empty, which would reopen
  every held position.
- The tracker fetches each market once however many arms hold it.
- The LLM audit file is written once, after the review phase, and also if a call crashes, so the
  spend of every finished call is kept.

`tests/test_request_budgets.py` pins these as call-count tests against a counting store.

## Modules (planned)

- `markets`: Gamma client, market model, category and fee rate lookup.
- `filters`: pure functions from market + book to pass/fail with raw values. Thresholds from config.
- `books`: CLOB client, snapshot storage, book-walk fill simulation (pure and unit-testable against stored books).
- `llm`: `review(snapshot, prompt, client, ...)` with strict schema validation, retry once then `error` (a reject). It returns the verdict and the full call record; the scanner writes all of a scan's records in one audit file, and a retried scan calls the LLM again (each attempt's spend stays in its own file). Prompts are versioned text files in `prompts/`, selected by id in the config `llm` section together with the pinned model id. `deepseek` is the thin provider adapter (`DEEPSEEK_API_KEY` from the environment); it returns the text with the API's token usage (cache hit, cache miss, output). `pricing` prices each attempt from `deepseek_pricing.yaml` (USD per 1M tokens, peak or off-peak by the UTC scan time, windows include their start and exclude their end; path in `BONDHYPE_PRICING`). Failed calls carry no usage and cost nothing; a rejected reply is still billed; a model missing from the price table leaves `cost_usd` null rather than failing the review. Records written before this change have no usage. No tools in v1.
- `arms`: `route(reject, buy)` returns the arms that trade a rules-passing candidate; only an explicit buy counts, `error` is a reject. Caps and dedup stay per arm in `entry`; rejected arms, and arms whose entry the portfolio refuses, are recorded as `rejected` / `refused:<reason>` in the decisions table, which keeps the market in cooldown so the LLM is not re-called every scan. The cooldown ends only when the ask is at least `portfolio.cooldown_price_move` from the ask at the arm's latest such row (either direction); there is no time expiry. A retried scan first drops the rows its earlier attempt left. Refusals are logged as `entry refused <arm>/<strategy> on <market>: <reason>`. Without an LLM client, only `baseline` trades.
- `portfolio`: balance, exposure, P&L per arm and strategy, derived from events.
- `ledger`: the trades table, loaded once per run into memory, where entry checks dedup and caps, and saved once (see S3 layout and Request cost).
- `decisions`: the decisions table and the cooldowns derived from it.
- `migrate`: one-off builder of both tables from the legacy per-record files (`python -m bondhype.migrate --bucket <bucket> --out <dir>` to check, `--write` to create them in the bucket; it refuses to overwrite existing tables).
- `settlement`: pure `resolve(position, raw_market)` and `assess_overdue(position, raw_market, now)`. A market settles only when Gamma says `closed`, `umaResolutionStatus == "resolved"`, outcome prices are exactly 1/0 and `closedTime` parses; anything else (disputed, 50/50, malformed) stays unsettled, so the conservative view keeps it as a total loss. `track` (hourly) records resolutions in the trades table and writes one price-path file per run; `overdue` (daily) records the day's analysis on each overdue trade. One failing market never blocks the others.
- `stats`: gate computations (net EV, confidence intervals, break-even margin, drawdown, vs-baseline test). Single source of truth for the verdict. Pure functions over positions, resolutions and overdue reports. `arm_stats` gives resolved-only and conservative views (overdue = total loss, ordered last for drawdown; capital cost charged pro rata by days held); EV intervals and the arm-vs-baseline test use a seeded bootstrap (vs-baseline is one-sided at `gate.confidence`, independent resampling). `verdict` evaluates the gate on the conservative view and computes only the primary comparisons (each LLM arm vs baseline). Thresholds live in the config `gate:` section.
- `reporting`: `build_report` reads the trades table, the decisions table and today's candidates files into per-arm/strategy figures (cumulative stats and gate verdict via `stats`; today's candidates = rule-passing selections, trades, resolutions; LLM error rate from the arm's prompts; latest overdue status per unresolved market). `format_report` renders the Telegram text with resolved-only and conservative views side by side and labels non-gate cuts exploratory. `check_heartbeat` alerts if the newest `candidates/` scan file is older than `health.heartbeat_max_age_minutes`. `telegram` is the thin Bot API adapter (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from the environment); `local_report` sends the report, heartbeat check or a test alert from a local run. The CloudWatch Lambda-error alarm is deployed in phase 8.
- `storage`: S3 read and write helpers, deterministic keys, Parquet schemas. `LocalStorage` and `S3Storage` share one contract test (S3 via moto).

## Key design decisions

- **Replayability:** the LLM sees stored snapshots only, and raw books are stored, so prompts and fill assumptions can be re-run on old candidates.
- **Honest fills:** taker-only, book-walked, fee from the market's category rate at entry. Slippage, fee and gross edge are separate fields.
- **Paired design:** every candidate that passes the rules goes to the buy prompt, and to the reject prompt too when the buy prompt says buy, so every arm sees the same data. A skipped reject prompt counts as no buy, which makes `prompt_reject` equal to `mix_and` and `mix_or` equal to `prompt_buy` by construction; all five arms are still recorded.
- **No silent defaults:** LLM errors are treated as rejects. Missing data fails closed.
- **Conservative accounting:** overdue positions count as total losses in the gate view.
- **No lost updates:** S3 has no transactions, so the jobs that write the trades table never run at the same time (scanner and tracker in sequence in one state machine, overdue at another time), and every table write is conditional on the version read.
- **Pinned everything:** model id, prompt versions, config version are pinned and recorded per record. The model is not changed mid-test.

## Open items

- Exact DeepSeek Flash model id and API terms to be pinned at build time.
- Prompt wording for the two prompts and the fixed risk-flag vocabulary.
- Exact hard-volume floor and annualised-yield threshold for bond.
- Telegram chat and bot setup.

## Deployment (phase 8)

- `handlers.py`: one `run_*` function per job over a `Runtime` (storage, config, LLM setup, Telegram `send`, API fetchers). Tracker and overdue send a Telegram alert listing any market that failed; the report is stored once per day (a rerun re-sends but keeps the first stored copy); `run_alarm` forwards CloudWatch alarm state changes from SNS. `lambda_entry.py` holds the one-line Lambda entry points; they use the event's `time` (the EventBridge schedule time, passed through by the state machine) as `now`, so a retried invocation redoes the same scan time.
- `runtime.build_runtime(env)`: bucket, config path and prompts dir from env; secrets are read from SSM SecureString parameters named in env (`{prefix}/deepseek-api-key`, `/telegram-bot-token`, `/telegram-chat-id`), created by hand and never in Terraform state. Missing settings fail fast.
- `Dockerfile`: one image for all functions; config and prompts are baked in, so an image pins both versions. Each function sets its own handler through `image_config.command`.
- `infra/` (Terraform): S3 bucket (versioned, encrypted, private), ECR, one least-privilege role per function (no `s3:DeleteObject`; only scanner, tracker, overdue and report may `PutObject`), the `bondhype-cycle` state machine (`infra/cycle.tf`: scanner then tracker, started on the hour), EventBridge schedules (heartbeat hourly, overdue 06:40 and report 07:00 UTC), optional reserved concurrency 1 on scanner and tracker, an Errors alarm per job feeding an SNS topic and the `alarm` function.
- Every function reads all three secrets because the shared runtime builds the LLM and Telegram clients, so the IAM grant is the same set of three parameters, not per-function.
- New AWS accounts often cannot reserve concurrency (the unreserved pool must stay at 10 or more). Request a limit increase or set `reserve_concurrency = false`; the state machine and the conditional writes already keep writers apart.

Deploy order: create the SSM parameters, `terraform apply -target=aws_ecr_repository.app`, build for `linux/amd64` and push the image to that repository, then a full `terraform apply`, then the 24 h dry run before freezing config and prompt versions.

**Continuous deployment** (`.github/workflows/deploy.yml`): each push to `main` (docs-only changes excluded) runs ruff and pytest, then builds the `linux/amd64` image, pushes it to ECR as `:latest` and `:<commit sha>`, and runs `update-function-code` on all six functions with `:latest` (the tag Terraform's `image_uri` already uses, so there is no drift). It assumes the `bondhype-deploy` role through GitHub OIDC (`infra/ci.tf`: trust limited to `refs/heads/main` of `github_repo`; ECR push on the one repository plus `UpdateFunctionCode` on the six functions, nothing else). Terraform is not run in CI because its state is local; changes under `infra/` (environment variables, schedules, IAM) still need a manual `terraform apply`, and must be applied before the push that depends on them. One-time setup: `terraform apply` to create the role, then set the repository variable `AWS_DEPLOY_ROLE_ARN` to the `deploy_role_arn` output. To roll back, re-point the functions at an earlier commit tag.

## Pulling results for local analysis

`python -m bondhype.pull --bucket <bucket>` mirrors S3 into `data/` with the same key layout, so `local_report`, `build_report` and `verdict` run on it unchanged. It is incremental and read-only on S3: the two tables are downloaded on every pull, and every other key is written once, so one already present locally is skipped. Raw `books/` and both price-path layouts are skipped unless `--heavy`, and old ledger checkpoints are never pulled (they are derived); `--prefix` (repeatable) narrows to specific prefixes. It uses your normal AWS credentials (`AWS_PROFILE`) and needs `s3:ListBucket` and `s3:GetObject`. `BONDHYPE_BUCKET` can replace `--bucket`.

## Scan phases, concurrency and logging

`scan` runs in three phases: (1) evaluate every market and collect candidates, (2) run the buy review for all candidates an LLM arm could still trade, then the reject review for the ones it said buy to, each phase concurrently in a thread pool of `llm.max_workers`, (3) open positions sequentially in market order, so fills, caps, cooldowns and the single-writer rule behave as before. Reviews are independent, so concurrency changes nothing about the results. A first scan with ~90 candidates makes ~180 calls (about 10 s each when sequential, over 30 minutes), so concurrency is what keeps it inside the Lambda limit (scanner timeout 900 s, the maximum). `llm.timeout_seconds` bounds each HTTP call (two attempts per review). Books are fetched in phase 1 and reused for entry, so on a heavy scan an entry book can be a few minutes old; every arm still uses the same snapshot.

Logging uses the standard `logging` module (INFO): Gamma pages fetched, progress every 100 markets, each candidate, each LLM call with verdict and duration, failed attempts as warnings, each position opened. `local_scan` prints them to the terminal; Lambda output goes to CloudWatch Logs.
