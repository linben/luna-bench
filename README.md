# luna-bench

A stdlib-only Python latency benchmark for GPT-5.6 Luna across Amazon Bedrock and OpenAI.
Supports streaming Chat Completions and Responses over HTTP/1.1, bounded load generation,
and recurring runs with a terminal dashboard. No SDK or Python dependencies required.

| Target | Base URL | Model |
|---|---|---|
| `bedrock-runtime` | `https://bedrock-runtime.{region}.amazonaws.com/openai/v1` | `global.openai.gpt-5.6-luna` (override with `--runtime-model`) |
| `bedrock-mantle` | `https://bedrock-mantle.{region}.api.aws/openai/v1` | `openai.gpt-5.6-luna` |
| `openai` | `https://api.openai.com/v1` | `gpt-5.6-luna` |

## Quick start

Use Python 3.13 with SSL support. Run commands from the checkout root:

```sh
git clone https://github.com/linben/luna-bench.git
cd luna-bench
python3 -m lunabench --help

# Preview requests without credentials, network calls, or output files.
python3 -m lunabench run --dry-run --targets openai --rounds 1 --warmup 0
```

Configure a provider below, then run a small benchmark:

```sh
# Billable: six measured requests (three scenarios × two APIs), no warmups.
python3 -m lunabench run --targets openai --rounds 1 --warmup 0
```

**All `run` and `probe` commands without `--dry-run` make billable provider requests.**
Check model access and the resulting error counts before increasing load or enabling recurring runs.

### Authentication

Choose an explicit `--targets` value; otherwise all three targets are selected.
The CLI reads the process environment, not `.env` files.

- **OpenAI:** set `OPENAI_API_KEY`; use `--targets openai`. Always uses bearer authentication
  and is skipped when the key is absent. In Bash, enter the key without shell-history exposure:
  ```bash
  read -rsp 'OpenAI API key: ' OPENAI_API_KEY; printf '\n'
  export OPENAI_API_KEY
  ```
- **Bedrock / SigV4 (default):** set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, plus
  `AWS_SESSION_TOKEN` for temporary credentials. Alternatively, configure an AWS CLI v2 profile
  through `AWS_PROFILE` that supports `aws configure export-credentials --format process`.
  Environment credentials take precedence; partial credentials are rejected. Complete SSO login
  when needed and ensure credentials remain valid for unattended runs.
- **Bedrock / bearer:** set `AWS_BEARER_TOKEN_BEDROCK` and pass `--auth bearer`.
  Setting the token alone does not change the authentication mode.

Use `--region` to override `AWS_REGION` / `AWS_DEFAULT_REGION`; the fallback is `us-east-1`.

## Running benchmarks

```sh
# Sustained concurrency: refill workers as requests finish.
python3 -m lunabench run --targets bedrock-runtime --apis chat \
    --rounds 100 --concurrency 8 --client-label us-east-1-raw-http1

# Fixed arrivals: 5 requests/s for 60 seconds, at most 8 active + 16 queued.
python3 -m lunabench run --targets bedrock-runtime --apis chat \
    --load-mode rate --arrival-rate 5 --duration 60 --concurrency 8 \
    --max-pending 16 --bucket 30

# Serial, low-rate probe; missed slots become dropped rows.
python3 -m lunabench probe --targets openai --scenarios extraction \
    --interval 15 --duration 3600 --bucket 300
```

- `--rounds` counts requests **per scenario per target/API**, independent of concurrency.
  `--duration` sets offered-load seconds per target/API instead; admitted work drains afterward.
- Load modes: `sustained` refills available workers; `rate` schedules independently of completions
  and records overload drops; `burst` waits for each batch. `--max-pending` applies only to `rate`.
- Defaults: three grounded scenarios, both APIs, 20 rounds, concurrency 1, one warmup round,
  sustained load, warm cache policy, and reasoning effort `none`. Warmups are excluded from metrics.
- `--cache-mode cold` places a unique marker before the prompt; `warm` preserves a stable prefix.
  Neither guarantees cache behavior. `--reasoning-effort default` leaves effort to the provider;
  `--max-output-tokens` overrides scenario caps, including reasoning where the provider counts it.
- Connections are reused per worker/host; `--fresh-connection` disables reuse. One stale-connection
  reconnect may replay an accepted POST; it is included in latency and is not a 429/5xx retry policy.
- `--timeout` is a socket inactivity timeout, **not a total request deadline**. Use `--client-label`
  to identify the client/network path. Targets and scenario order are seeded; repeat across time/seeds.

See `python3 -m lunabench run --help` for all options.

### Recurring runs and TUI

```sh
# Run immediately, then every 600 seconds. Add --no-tui for headless operation.
python3 -m lunabench run --daemon --targets openai --rounds 1 --warmup 0 --interval 600
```

The dashboard appears on interactive terminals with curses support; otherwise output is line-oriented.
`q`, Escape, Ctrl-C, or SIGTERM stops new work and drains admitted requests before writing the report.
Slow suites never overlap: missed start slots are skipped. Failed cycles are logged and the next slot
still runs. Normal shutdown exits `0`; scheduler/logging failures exit nonzero, unlike benchmark failures.

The daemon stays in the foreground; use tmux or a service manager to survive logout. Shutdown can wait
indefinitely for a stream that keeps sending data. Each session gets a new `results/daemon-<UTC>-<id>/`
directory. `--log-dir` must name a new directory; `--out` and `--dry-run` cannot be used with `--daemon`.

## Workloads

Grounded tasks use a frozen [public-source fixture packet](lunabench/grounded_fixtures.json)
with source IDs, URLs, and provenance. These are **Exa-inspired model-stage workloads, not production
traffic replay**. No retrieval calls or complete retrieval/agent workflows are measured.

| Scenario | Cases | Task | Output cap | Validation |
|---|---:|---|---:|---|
| `extraction` | 4 | Structured fact extraction, including missing evidence | 1024 | Strict JSON and exact source-backed facts |
| `answer` | 4 | Grounded answer, 140–350 words | 2048 | Length and citation references |
| `synthesis` | 3 | Multi-document brief, 400–800 words | 4096 | Length and citation references |
| `short` | 1 | Synthetic intent classification | 1024 | Allowed label |
| `medium` | 1 | Synthetic ~8,000-character input, three bullets | 1024 | Bullet format |
| `long` | 1 | Synthetic ~32,000-character input, five sentences | 2048 | Sentence format |

**Prose validation is structural, not factual verification.** Passing citation and length checks does
not establish correctness. Cases rotate deterministically; workload fingerprints separate changed
fixtures, prompts, schemas, and validation settings. Compare actual provider-reported token counts.

## Results and measurements

Runs write a new `results/run-<UTC>.jsonl` (or `--out` path) and a sibling `.jsonl.md` report.
Daemon sessions contain `daemon.jsonl` lifecycle events and per-cycle `run-*/samples.jsonl` files/reports.
Output files are never appended to or overwritten. To combine samples:

```sh
python3 -m lunabench report results/*.jsonl --bucket 300 --markdown results/combined.md
```

Use a new `--markdown` destination. For daemon data, pass the session's `run-*/samples.jsonl` files,
**not `daemon.jsonl`**. Event logs rotate at 10 MiB with five backups; samples/reports are not pruned.

| Metric | Meaning |
|---|---|
| `t_connect_ms`, `t_tls_ms` | DNS+TCP and TLS durations when opening a connection |
| `ttfb_ms` | Response status and headers received |
| `ttft_ms`, `t_last_text_ms` | First/last nonempty, complete SSE text event—not model tokens |
| `t_done_ms`, `t_eof_ms`, `total_ms` | Terminal signal, clean HTTP EOF, and transport return/failure |
| `queue_ms`, `client_ttft_ms`, `client_total_ms` | From scheduled arrival to worker start, first text, and completion including validation |
| `itl_ms`, `max_stall_ms`, `stall_count` | Inter-text-event gaps, maximum gap, and gaps strictly over 100 ms |
| `text_delivery_chars_per_s` | Post-first-delta character delivery rate, not token-generation throughput |

Transport times start at the logical request, including reconnects. Non-200 responses close after
headers without reading error bodies. Token usage preserves unknown values as null. A single text
delta has no inter-text maximum or delivery rate; first-text and trailing silence are separate metrics.

A validated success requires HTTP 200, clean completion, nonempty output, and scenario validation.
Chat requires `finish_reason=stop` and `[DONE]`; Responses requires `response.completed`. Refusals,
truncation, malformed streams, and missing terminal markers are failures, not successful latency samples.

Reports separate models and workload/client configurations, show p50/p95/p99/max with measurement
counts, and count failures/drops separately. Legacy data stays explicitly unvalidated.
**Successful-only latency is not an all-arrival SLO**, and small-sample tail warnings are not confidence
bounds. Observed RPS uses each cohort's scheduled-arrival-to-last-client-finish window, including drain
and failures—not reciprocal median latency or configured arrival rate.

## Development and security

```sh
python3 -m unittest discover -s tests -v
```

CI runs offline tests and credential-free previews on Python 3.13/3.14, plus checkout/history secret
scanning. Actions are commit-pinned with read-only permissions; Dependabot checks action updates.

- Never commit credentials or private data. Results, logs, `.env` files, and common private keys are
  ignored; ignores do not remove tracked history. Scan before sharing and rotate exposed credentials.
- New samples, reports, and event logs use owner-only permissions (`0600` on POSIX); daemon directories
  use `0700`. Raw output, provider error bodies, and arbitrary exception messages are not persisted.
- Request IDs, configuration, labels, and paths can still be sensitive. Existing artifacts are not
  sanitized. Review exports; do not publish an archive of the entire working directory.

## License

Project code and original documentation are licensed under the [MIT License](LICENSE),
copyright © 2026 Ben Lin.

The eight third-party source excerpts in `lunabench/grounded_fixtures.json` are **not covered by this
MIT grant**. Their source attribution and provenance are retained in that file; redistribution remains
subject to the respective rights holders' terms and required notices. Public availability alone does
not establish redistribution permission.
