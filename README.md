# luna-bench

Stdlib-only Python 3.13 latency benchmark for GPT-5.6 Luna across three endpoints:

| target | URL | model |
|---|---|---|
| `bedrock-runtime` | `https://bedrock-runtime.{region}.amazonaws.com/openai/v1` | `global.openai.gpt-5.6-luna` (`--runtime-model`) |
| `bedrock-mantle` | `https://bedrock-mantle.{region}.api.aws/openai/v1` | `openai.gpt-5.6-luna` |
| `openai` | `https://api.openai.com/v1` | `gpt-5.6-luna` |

Both streaming `/chat/completions` and `/responses` are supported. The client uses raw HTTP/1.1 via
`http.client`, not the OpenAI SDK. Bedrock defaults to in-process SigV4 (`AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, or `aws configure export-credentials`); `--auth bearer` uses
`AWS_BEARER_TOKEN_BEDROCK`. OpenAI uses `OPENAI_API_KEY` and is skipped when unset.

Repository: [linben/luna-bench](https://github.com/linben/luna-bench).
The Python package and CLI remain `lunabench` (`python3 -m lunabench`).

## Public release and security

Do not publish local benchmark artifacts. `results/` is ignored except for `.gitkeep`, as are
JSONL/log files, Python bytecode, environment files, and common private-key files. Ignore rules
do not remove previously tracked files or secrets from Git history. Review the staged file list
and run a secret scanner against both the checkout and full history before the first push.
If a real credential was exposed, revoke/rotate it; deleting its current file is not sufficient.
Do not upload a ZIP of this working directory: ignored local artifacts are still present.

Publication requires the owner's approval of the configured model identifiers and permission to
release this code. No code license has been selected; add an owner-approved license before offering
the project as open source. The eight attributed source excerpts in `grounded_fixtures.json` retain
third-party wording. Public URLs and citations establish provenance, not redistribution permission:
verify each source's terms and include required notices before release. Do not assume a project
license covers third-party text. Use only authorized, non-confidential fixtures and client labels.

Samples and reports are created exclusively with owner-only permissions (0600 on POSIX), as are
daemon event logs; daemon directories use 0700. Existing artifacts are not permission-migrated or
sanitized. Provider error bodies, arbitrary stream error messages, and raw exception messages are
not saved: errors retain local categories and HTTP status instead. Tracebacks retain code locations
without source lines, absolute source paths, or exception contents. Request identifiers, configuration,
user-supplied labels/paths, and workload metadata can still be sensitive; review any artifact before sharing.

GitHub Actions runs the offline tests and credential-free preview on Python 3.13/3.14, plus a
checksum-pinned Gitleaks scan of the checkout and full Git history. Actions are commit-pinned with
read-only permissions and no persisted checkout credentials; Dependabot checks action updates.
Enable GitHub secret scanning/push protection and require CI on protected branches where available.
Automated scans reduce risk but cannot certify that all confidential information is absent.

## Setup and easy start

The CLI is already included: **`python3 -m lunabench`**. Run it from this repository's root.
No `pip install`, virtual environment, SDK or separate launcher is required. Daemon mode runs the
benchmark workloads against model providers, **not** `unittest`; real runs incur provider charges.

### 1. Check Python and terminal support

Use Python 3.13 with SSL support. On Debian 13, the prerequisites can be installed with:

```sh
sudo apt-get update
sudo apt-get install python3 ca-certificates ncurses-base
```

Clone the repository, then check the interpreter and CLI (or change to your existing checkout):

```sh
git clone https://github.com/linben/luna-bench.git
cd luna-bench
python3 --version
python3 -c 'import ssl, curses; print("SSL and TUI support available")'
python3 -m lunabench --help
python3 -m lunabench run --help
```

Use a normal interactive terminal for the TUI. In SSH sessions, connect with `ssh -t` if necessary.
Missing curses/terminal support does not prevent headless operation: add `--no-tui`. You also need
outbound HTTPS/DNS access to the selected provider, model access in that account/region, and writable
disk space for `results/`. The benchmark does not configure accounts, grant model access, or install
AWS tooling for you.

### 2. Configure one provider

Start with **one explicit `--targets` value**. Without it, all three targets are selected, including
AWS targets that require AWS credentials. The following interactive secret-entry examples use Bash;
keys are not entered into shell command history. Do not commit credentials to this repository.

**OpenAI:** obtain an API key for an account with access to the configured model.

```bash
read -rsp 'OpenAI API key: ' OPENAI_API_KEY; printf '\n'
export OPENAI_API_KEY
```

Use `--targets openai`. This target always uses bearer authentication; no `--auth` flag is needed.

**Bedrock with AWS credentials (SigV4, the default):** use an existing AWS CLI v2 profile whose
credentials can be exported by `aws configure export-credentials --format process`.

```sh
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
# For an SSO-backed profile, log in before starting the benchmark:
aws sso login --profile "$AWS_PROFILE"
```

The SSO login is only for SSO profiles. For other profiles, use your normal AWS credential setup.
The benchmark invokes the AWS CLI when environment credentials are absent; ensure `aws` is on PATH.
It does not implement the full AWS credential chain itself. Alternatively, supply credentials directly:

```bash
read -rp 'AWS access key ID: ' AWS_ACCESS_KEY_ID
read -rsp 'AWS secret access key: ' AWS_SECRET_ACCESS_KEY; printf '\n'
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export AWS_REGION=us-east-1
# Required when using temporary session credentials:
read -rsp 'AWS session token: ' AWS_SESSION_TOKEN; printf '\n'
export AWS_SESSION_TOKEN
```

For long-lived credentials, omit the session-token prompt and unset any stale `AWS_SESSION_TOKEN`.
Environment credentials take precedence over profiles. Expired environment credentials are not
automatically replaced; SSO sessions may also require a fresh login. Choose a credential source that
remains valid for the intended unattended run. Use `--targets bedrock-runtime`, `bedrock-mantle`, or
`bedrock-runtime,bedrock-mantle`, with permissions for the selected model/endpoints. `--region` overrides
`AWS_REGION`/`AWS_DEFAULT_REGION`; the fallback region is `us-east-1`.

**Bedrock with a bearer API key:** if your account/endpoint supports this authentication method:

```bash
read -rsp 'Bedrock bearer token: ' AWS_BEARER_TOKEN_BEDROCK; printf '\n'
export AWS_BEARER_TOKEN_BEDROCK
export AWS_REGION=us-east-1
```

Use `--targets bedrock-runtime --auth bearer` (or the other Bedrock target). Setting this token alone
does not select bearer authentication; **include `--auth bearer`**.

### 3. Preview, then make one small real run

These examples use OpenAI; substitute your chosen target and authentication flags from above.
First inspect the request bodies without credentials, network calls, output files or charges:

```sh
python3 -m lunabench run --dry-run --targets openai --rounds 1 --warmup 0
```

Then run one small suite. **This command makes billable requests:** three grounded scenarios across
two APIs, one request per scenario/API, totaling six measured requests and no warmups for one target.

```sh
python3 -m lunabench run --targets openai --rounds 1 --warmup 0
```

Check the printed sample/report paths and unsuccessful counts before leaving a daemon running.
Previewing confirms request construction only, not credentials, permissions or provider availability.

### 4. Start easy mode with a TUI

Reuse the working small-suite configuration and add `--daemon`:

```sh
# Immediate first suite, then a start slot every 600 seconds (10 minutes).
python3 -m lunabench run --daemon --targets openai --rounds 1 --warmup 0

# Optional: use a five-minute interval instead. Run this instead of the command above.
python3 -m lunabench run --daemon --interval 300 --targets openai --rounds 1 --warmup 0
```

The TUI appears automatically on an interactive terminal. Press `q`, Escape or Ctrl-C to stop after
admitted requests drain. A suite that overruns its interval never overlaps another suite: missed slots
are skipped. The first command offers up to 36 measured requests/hour after startup when suites finish
within ten minutes. Costs depend on input/output tokens and provider pricing, not just request counts.

The shortest command, `python3 -m lunabench run --daemon`, also works once all selected providers are
configured, but uses the larger defaults: 20 rounds, three scenarios, two APIs and one warmup round.
With all three targets available, that is 360 measured requests plus 18 warmup requests per cycle.
Start with the explicit small-suite commands above, then increase load intentionally.

### 5. Keep it running after disconnecting

**Keep the interactive TUI with tmux:** install tmux if needed, then:

```sh
tmux new -s lunabench
# Inside tmux, change to the checkout and configure credentials as in step 2.
python3 -m lunabench run --daemon --targets openai --rounds 1 --warmup 0
```

Detach with Ctrl-B, then D. Reattach with `tmux attach -t lunabench`. Stop with `q` from the dashboard.
An existing tmux server may have an older environment; set credentials inside the session if needed.

**Use a Linux systemd user service for unattended/headless operation:** the example below uses OpenAI.
First prepare files outside the repository; use your preferred editor to fill them in:

```sh
mkdir -p ~/.config/lunabench ~/.config/systemd/user
chmod 700 ~/.config/lunabench
touch ~/.config/lunabench/environment
chmod 600 ~/.config/lunabench/environment
```

Put this in `~/.config/lunabench/environment`, replacing the value with your real key. This is a systemd
environment file, not a shell script: use `NAME=value` lines, without `export` or shell substitutions.
This file is **not** automatically loaded by the interactive CLI.

```ini
OPENAI_API_KEY=your-real-api-key
```

Save the following as `~/.config/systemd/user/lunabench.service`. Replace `WorkingDirectory` with the
absolute checkout path and `/usr/bin/python3` with the absolute path to your Python 3.13 if different.
Do not leave the placeholder path unchanged.

```ini
[Unit]
Description=luna-bench recurring model benchmark

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/luna-bench
EnvironmentFile=%h/.config/lunabench/environment
ExecStart=/usr/bin/python3 -m lunabench run --daemon --no-tui --interval 600 --targets openai --rounds 1 --warmup 0
Restart=on-failure
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=infinity
UMask=0077

[Install]
WantedBy=default.target
```

For AWS, change `--targets`/`--auth` in `ExecStart` and put the relevant AWS variables in the environment
file instead. User services do not source `.bashrc` or inherit newly exported shell variables. For AWS
profile authentication, make sure the service PATH includes the installed AWS CLI and that the profile
is accessible to this user. Do not put keys directly in `ExecStart` or the unit file.

Enable and start the service only when ready for recurring **billable** requests:

```sh
systemctl --user daemon-reload
systemctl --user enable --now lunabench.service
systemctl --user status lunabench.service
journalctl --user -u lunabench.service -f
```

Exit the journal viewer with Ctrl-C; this does not stop the service. To stop or change settings:

```sh
systemctl --user stop lunabench.service
# After editing the unit, reload it; then start again when ready.
systemctl --user daemon-reload
systemctl --user start lunabench.service
# Disable automatic startup and stop it:
systemctl --user disable --now lunabench.service
```

If the service must start at boot and remain running without a login, ask your administrator to enable
lingering for your account (`sudo loginctl enable-linger "$USER"`). This keeps the user service manager
running outside login sessions. A user systemd manager is required; containers without one can use an
existing supervisor or tmux instead. `TimeoutStopSec=infinity` preserves graceful draining but means a
stuck request can delay shutdown indefinitely; a finite timeout may kill unfinished requests and lose
their final samples/report. Configure that tradeoff deliberately.

Do **not** use a fixed `--log-dir` in an automatically restarted service: directories must be new.
Omitting it creates a unique session under the checkout's `results/` directory on each process start.
`Restart=on-failure` restarts daemon failures; provider/test failures are already handled on the next
scheduled cycle and do not restart the process.

### 6. Inspect results and troubleshoot

The TUI and service journal name the session directory. Open `daemon.jsonl` for lifecycle/request
events, and each `run-*/samples.jsonl.md` for a report. To combine every run in **one** session:

```sh
python3 -m lunabench report results/daemon-REPLACE-WITH-SESSION/run-*/samples.jsonl \
    --markdown results/session-combined.md
```

Replace the session directory with the actual printed path. The shell expands the sample glob; do not
include `daemon.jsonl` in `report` input, because it contains events rather than benchmark samples.
Event logs rotate at 10 MiB with five backups. Per-run samples and reports remain until you archive or
delete them; monitor free disk space. Reports from tiny starter runs cannot establish reliable tail latency.

| Symptom | Action |
|---|---|
| `No module named lunabench` | Run from the checkout root; check the service's `WorkingDirectory`. |
| No TUI, only lines of status | Check stdin/stdout are terminals and curses/`TERM` are available; headless fallback is intentional. |
| `no AWS credentials` during an OpenAI-only setup | Add `--targets openai`; the default also selects AWS targets. |
| `no available targets` / missing key | Set the variable for the selected provider in the process environment; the CLI does not load `.env` files. |
| HTTP 401/403 or model-access errors | Check credentials, expiry, permissions, region and actual model availability; dry-run cannot verify these. |
| Existing log directory error | Omit `--log-dir` or choose a new path; don't reuse a previous session directory. |
| Service works manually but not under systemd | Check environment file, absolute paths, AWS CLI PATH/profile access, and journal output. |
| Cadence slots skipped | The full suite exceeds the interval; increase it or reduce targets/APIs/rounds. |
| Stop waits a long time | Active requests are draining; the socket timeout is not an end-to-end deadline. |

See [Recurring daemon and TUI](#recurring-daemon-and-tui) for log fields, cycle status and shutdown semantics.

## What this represents

The default workloads are **Exa-inspired model-stage benchmarks, not Exa production replay**.
They use a frozen, curated public-source packet with source IDs, titles, URLs, excerpts, and provenance
in `lunabench/grounded_fixtures.json`. No retrieval calls occur during a benchmark.

| scenario | cases | output task | token ceiling | validation |
|---|---:|---|---:|---|
| `extraction` | 4 | Structured fact extraction, including unsupported/missing values | 1024 | Strict JSON shape and exact expected source-backed facts |
| `answer` | 4 | Grounded answer, 140–350 words | 2048 | Word count and required/known citation IDs only |
| `synthesis` | 3 | Multi-document brief, 400–800 words | 4096 | Word count and required/known citation IDs only |
| `short` | 1 | Synthetic one-word intent classification | 1024 | Allowed label only |
| `medium` | 1 | Synthetic approximately 8,000-character input, three short bullets | 1024 | Bullet format only |
| `long` | 1 | Synthetic approximately 32,000-character input, five sentences | 2048 | Sentence format only |

**Prose validation is not factual verification.** A completed answer passing citation/length checks
can still be incorrect. `validation_scope` makes that limitation explicit in each row and report.
Grounded prompts have natural corpus sizes, not claimed token bins; compare actual provider-reported
input/output/reasoning tokens. Caps include reasoning where the provider counts it; increase
`--max-output-tokens` when exploring higher effort instead of accepting truncation as success.
Reasoning defaults to the explicit `none` control. `--reasoning-effort default` omits the field and
leaves the choice to the provider; it is not a claim about Exa's settings.

Source motivation: Exa documents [per-result summaries, synthesis and iterative research](https://exa.ai/docs/search/best-practices)
and [answers with citations and structured outputs](https://exa.ai/docs/reference/answer).
The fixture also includes HTTP and SSE standards. To claim production representativeness, calibrate
against authorized redacted traces: actual templates/schemas, joint token distributions, cache reuse,
arrival rates, fan-out/dependencies, client location, SDK/proxy path, retry policy and quality criteria.
This bench does not measure a complete retrieval/tool/agent workflow or simulate those dependencies.

## Usage

```sh
python3 -m unittest discover -s tests -v

# Inspect all default request bodies: no credentials, network, inference, or output file.
python3 -m lunabench run --dry-run --targets bedrock-runtime

# Small real-provider smoke: one request per grounded scenario and API, no warmup.
# Running without --dry-run makes billable inference requests.
python3 -m lunabench run --targets bedrock-runtime,bedrock-mantle \
    --rounds 1 --warmup 0 --client-label us-east-1-raw-http1

# Sustained load: refill every available worker, 100 requests per scenario per target/API.
python3 -m lunabench run --targets bedrock-runtime,bedrock-mantle --apis chat \
    --rounds 100 --concurrency 8 --cache-mode warm --client-label us-east-1-raw-http1

# Fixed arrivals: 5 requests/s over the scenario mix for 60 seconds PER target/API.
# At most 8 active + 16 queued requests; excess arrivals are recorded as dropped.
python3 -m lunabench run --targets bedrock-runtime,bedrock-mantle --apis chat \
    --load-mode rate --arrival-rate 5 --duration 60 --concurrency 8 --max-pending 16 \
    --client-label us-east-1-raw-http1 --bucket 30

# Isolated bursts remain available; cold prefixes and cold connections are independent.
python3 -m lunabench run --load-mode burst --rounds 10 --concurrency 8 \
    --scenarios short,medium,long --cache-mode cold --fresh-connection

# Serial low-rate probe; a missed slot becomes a dropped row, not a hidden omission.
python3 -m lunabench probe --scenarios extraction --interval 15 --duration 3600 --bucket 300

# Re-render or merge; old JSONL remains readable as explicitly unvalidated legacy data.
python3 -m lunabench report results/*.jsonl --bucket 300 --markdown results/combined.md
```

`run` defaults to the three grounded scenarios, both APIs, concurrency 1, 20 rounds, one warmup round,
`--load-mode sustained`, and `--cache-mode warm`. `--rounds` now means requests **per scenario per
target/API**, independent of concurrency. `--duration` instead sets the offered-load duration per
target/API; admitted requests drain afterward. `--rounds` and `--duration` are mutually exclusive.
Targets run in seeded shuffled order; APIs run in the selected order. Repeat with different seeds/time
windows to avoid confusing time-of-day effects with endpoint differences.

Within each target/API cohort, scenarios have equal weight in a seeded order; cases rotate deterministically.
The same request indices select the same cases across endpoints, although unique request markers differ.
A cohort contains concurrent independent model calls, not a user-workflow fan-out simulation.
Warmup exercises the selected templates and is excluded from recorded metrics. It warms connections
and may warm prefixes; it does not guarantee every worker/prefix was warmed. Set `--warmup 0` to observe
startup. Fixed-rate scheduling does not stop generating arrivals when the service slows down;
`--max-pending 0` means no queue beyond active workers. A probe is serial diagnostic traffic, not a load test.

### Recurring daemon and TUI

```sh
# Run the selected benchmark suite immediately, then every 10 minutes.
python3 -m lunabench run --daemon

# Five-minute cadence; all normal run workload/load options still apply.
python3 -m lunabench run --daemon --interval 300 \
    --targets bedrock-runtime,bedrock-mantle --rounds 1 --warmup 0

# Foreground service mode, without curses. The log directory must be new.
python3 -m lunabench run --daemon --no-tui --log-dir results/daemon-nightly
```

`--interval` is a positive number of **seconds**, default `600`. Each cycle runs the same selected
benchmark workload as a normal `run`, including warmups; this is not the Python unit-test suite.
It makes billable inference requests. Scheduling uses a monotonic start-to-start cadence. A slow suite
never overlaps the next one: missed slots are logged and skipped, not queued or replayed. Failed runs
are logged and the next scheduled cycle proceeds; there are no extra request retries. Each cycle
refreshes credentials through the existing credential provider.

On interactive stdin/stdout, a stdlib curses TUI shows current target/API, sample/error/drop counts,
elapsed time, next-run countdown, settled/failed cycle counts, artifact paths and recent events. It
handles terminal resizing. `q`, Escape, Ctrl-C and SIGTERM stop scheduling and drain admitted requests
before writing the partial report. Shutdown can take as long as those requests take; `--timeout` remains
a socket inactivity timeout, not a total shutdown deadline. Warmups are logged but excluded from measured
sample counts. Completed counts all settled cycles; failed counts exceptions, nonzero runner exits, or
cycles with unsuccessful measured samples (including drops). Interrupted cycles are labeled separately.

The process stays in the foreground: use a service manager, tmux or your usual process supervisor for
unattended operation. `--no-tui` explicitly selects line-oriented status output; redirected stdin/stdout
or unavailable curses also select it automatically. Normal requested shutdown exits `0`; scheduler or
logging failure exits nonzero. Benchmark failures remain visible in cycle status and logs, not daemon
exit status.

Artifacts live in a new private `results/daemon-<UTC>-<unique-id>/` directory, or the new directory
specified by `--log-dir`. Existing directories are rejected to prevent log rotation races or overwrites.

- `daemon.jsonl`: UTC structured events with effective configuration, PID, cycle/run IDs, schedule
  skips, target skips, warmup results, every measured sample, summaries, exception categories and code locations,
  stop reason and artifact locations. Events are flushed immediately; cycle lifecycle events are also
  synced to disk. The log rotates at 10 MiB with five backups (`daemon.jsonl.1` through `.5`).
- `run-<sequence>-<UTC>-<unique-id>/samples.jsonl`: exclusive per-cycle sample data, flushed per row.
- `run-<sequence>-<UTC>-<unique-id>/samples.jsonl.md`: per-cycle report, including interrupted runs.
  A failure before benchmark initialization may have neither artifact; the lifecycle log records this.

Credentials and authorization headers are not intentionally logged, nor is raw generated output.
Provider error bodies and arbitrary exception details are omitted; local failure categories and
validation reasons remain. Treat configuration, request IDs, labels and artifact paths as sensitive.
Session/run directories are owner-only; event logs, samples and reports are owner-readable/writable.
Event-log I/O failure stops new work and drains admitted requests rather than silently continuing
without an audit trail. Artifacts are **not pruned**; monitor disk space and archive/remove old sessions.
`--out` and `--dry-run` cannot be combined with `--daemon`; preview a normal `run --dry-run` first.

### Cache and connection controls

- `--cache-mode warm`: stable instructions and source packet precede the varying query/request marker.
- `--cache-mode cold`: a unique nonce precedes the user prompt. System/schema prefixes can still be shared.
- These are prefix placement policies, **not cache-hit guarantees**. Check reported `cached_tokens`;
  absent usage is unknown, not zero. Retrieval/page caching is a different mechanism and is not exercised.
- Connections normally reuse one socket per worker/host; `--fresh-connection` forces fresh DNS/TCP/TLS.
- One stale pooled-connection reconnect is allowed and included in logical-request latency.
  Retrying a POST after disconnect may replay an accepted request; `reconnected`, `reconnect_error`
  and `reconnect_ms` expose that ambiguity. This is not a production HTTP-429/5xx retry policy.
- `--timeout` is a socket inactivity timeout, **not** an end-to-end request deadline.
- Set `--client-label` to distinguish client deployment/network paths. Reports warn when it is unspecified.

## Measurements and success criteria

New rows carry `schema_version=2`, run/request/fixture identities, a workload fingerprint covering
all fixture cases and prompt/schema/validation settings, the workload mix, and complete run configuration.
JSONL is written incrementally to a **new** `results/run-<UTC>.jsonl` or `--out` path; existing files are
never appended to or overwritten. Reports are created exclusively beside it as `<file>.md`;
`report --markdown` also requires a new destination. Raw output text is used in memory for validation and not
persisted; usage, character counts, failure categories and validation reasons remain.

Non-200 responses are closed after their headers without reading the body. Their `total_ms` measures
time to rejection, not error-body download time; failed rows remain excluded from successful latency.

Transport times are milliseconds from one logical request start, including connection setup/reconnect:

- `t_connect_ms`, `t_tls_ms`: measured DNS+TCP and TLS handshake durations, only when opened.
- `ttfb_ms`: status line and headers received, not the first answer text.
- `ttft_ms`, `t_last_text_ms`: first/last nonempty **complete SSE text event**, dispatched at its delimiter.
- `t_done_ms`: explicit terminal signal; `t_eof_ms`: actual clean HTTP EOF.
- `total_ms`: return/failure after stream consumption; distinct from semantic completion.
- `itl_ms`: inter-text-event gaps, **not token intervals**.
- `max_stall_ms`, `stall_count`: maximum gap and count strictly over 100 ms. A single-delta answer has
  no measurable inter-text maximum and zero stalls; first-text and trailing silence are separate.
- `text_delivery_chars_per_s`: characters after the first delta divided by first-to-last-text duration;
  absent for one delta or zero duration. This is delivery rate, not model decoding throughput.
- `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`: provider usage, preserving
  unknown counters as null. Misleading `output_tps` was removed.

Client timings start at the scheduled arrival: `queue_ms` ends at worker start; `client_ttft_ms` includes
queue and request preparation; `client_total_ms` includes those plus output validation. Rate-mode dropped
arrivals have scheduling metadata but no HTTP/transport measurements.

A validated success requires HTTP 200, clean protocol/transport completion, nonempty text and passed
scenario validation. Chat requires `finish_reason=stop` and `[DONE]`; Responses requires
`response.completed`. Refusals, output limits, failed/incomplete events, missing terminal markers,
malformed framing, stream errors and empty output are not successful latency samples. Usage already
received is retained on failure. SSE line/event/output limits fail explicitly rather than silently truncating.

## Reading reports

Reports isolate workload/configuration differences, including workload mix, model within target, region,
authentication, reasoning, cache/connection policy, arrival mode/rate/queue, concurrency, timeout, client
label, seed and schema version. Cross-target ratios name both models and require matching configurations.
Different authentication configurations are deliberately not compared automatically.

Latency includes only validated v2 successes; failures, invalid completions, drops and legacy rows are
counted separately. **Successful-only latency is not an all-arrival SLO.** Read error/drop rates beside it.
Reports show p50/p95/p99/max, measurement counts, per-request maximum stalls and stalled-request fraction.
Small-sample warnings are minimum-count warnings, not confidence guarantees: even 100 samples do not
reliably estimate a population p99. Repeat across time and collect substantially more samples for tails.

Observed RPS uses each run/target/API cohort's scheduled-arrival-to-last-client-finish window, including
failures/drops and drain time. Mixed workloads share that window; separately configured rows show their
contribution to total cohort throughput, not their own active-phase rate. It is not the configured arrival
rate or the reciprocal of median latency. Time buckets preserve configuration/model boundaries and full dates.
Old JSONL remains explicitly unvalidated legacy data; corrected v2 metrics are never silently mixed with it.

## Changes from the original bench

- Grounded, varied, longer-output profiles replace synthetic short/medium as defaults; controls remain available.
- Sustained refill and bounded fixed-rate arrivals complement explicit burst/probe modes.
- SSE event framing, semantic outcomes, retry clocks and worker connection cleanup are corrected.
- Failed/empty/truncated output cannot count as a validated success; extraction checks source-backed facts.
- Misleading tokens/sec and pooled-gap-only reporting are replaced by delivery/stall/client measurements.
- Reports separate configurations and models; historical results are preserved and labeled, not rewritten.
- Optional foreground daemon mode repeats suites on a configurable cadence with a live TUI, graceful
  shutdown, isolated per-cycle artifacts, and rotating structured lifecycle/request logs.
- Public-release safeguards exclude local artifacts, restrict output permissions, omit untrusted error
  payloads, and scan checkout/history in CI; release rights and model-name approval remain owner decisions.
- Report aggregation avoids redundant sorting and recomputation; overloaded arrivals skip unused prompt
  construction. Stall fractions honor the recorded unrounded threshold count rather than rounded gaps.
- Malformed report rows fail with file/line context, and admitted results drain without masking the
  original error or interruption.
