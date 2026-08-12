# Contributing Agent Tasks

Contribute your own agent tasks and we test if the agent solves them for CI testing!

## How to Add a Task

1. Create a new `.yaml` file in this directory (`tests/agent_tasks/`).
2. Use the following format:

```yaml
name: My Task Name
source_id: stable_catalog_source_id
task: Describe the task for the agent to perform
judge_context:
  - List criteria for success, one per line
max_steps: 10
```

## Guidelines
- Be specific in your task and criteria.
- Register the source in `tests/data_sources.yaml` and use its stable `source_id`.
- The `judge_context` should list what counts as a successful result.
- The agent's output will be judged by an LLM using these criteria.

## Data Source Levels

- `behavioral`: a public, repeatable source with a matching browser-agent YAML task.
- `availability`: a login-gated, JavaScript-heavy, region-sensitive, or anti-bot source checked for reachability without pretending it is a deterministic agent scenario.

Validate the catalog and task mapping locally:

```bash
uv run pytest tests/ci/test_data_source_integrity.py
uv run python -m scripts.check_data_sources
```

Use `--strict` when every availability-only source must also return one of its catalogued HTTP statuses:

```bash
uv run python -m scripts.check_data_sources --strict
```

Run the same catalog through isolated real Chromium sessions to distinguish reachability from model-actionable DOM state:

```bash
uv run python -m scripts.check_browser_data_sources \
	--concurrency 1 \
	--strict \
	--gate-mode actionability \
	--output /tmp/browser-use-browser-probe.json
```

Use `--disable-sandbox` only inside a trusted container or on a CI host where Chromium user namespaces are unavailable.
Prefer `--executable-path /path/to/google-chrome` when the bundled Chromium cannot use the host sandbox. The probe performs one shared
launch preflight before scheduling sites, requires consecutive stable DOM captures, ignores empty app roots, and records separate
`reachable`, `target_matched`, `content_available`, and `actionable` evidence. Source-specific browser contracts can constrain final paths,
titles, rendered content markers, visible text, meaningful elements, and interactive controls. Use `--gate-mode reachability`, `content`,
or `actionability` to select the required evidence level; `catalog` preserves the source's behavioral/availability contract.

For production anti-bot workloads, `--use-cloud` provisions a Browser Use Cloud browser and requires `BROWSER_USE_API_KEY`. Optional
`--cloud-profile-id`, `--cloud-proxy-country-code`, and `--cloud-timeout-minutes` values are forwarded to the cloud profile. Hosted browsers
are optimized for browser automation, captcha and bot-detection handling, remote profile synchronization, and low-latency execution.
Robots-denied sources remain excluded from crawl traversal regardless of browser runtime.

Run a robots-aware, read-only crawl experiment over safe same-origin links:

```bash
uv run python -m scripts.crawl_data_sources \
  --max-pages-per-source 4 \
  --max-depth 1 \
  --output /tmp/browser-use-crawl.json
```

Use repeated `--source`, `--category`, or `--test-level` filters for targeted follow-up runs. The crawler excludes
login, logout, registration, deletion, unsubscription, and Cloudflare email-protection routes from link traversal.
Every requested source ID must exist and the combined filters must select at least one source. Each queued URL is
checked against its origin's robots policy, public-network policy, and read-only route policy before fetching.
Cross-origin redirects are recorded and stopped; they never establish a new crawl origin. Redirect targets are
revalidated, private/loopback/link-local destinations are rejected, and tracking-only query parameters are removed
from URL identity. Use `--allow-private-networks` only for isolated local fixtures.

The command calculates a quality gate for behavioral sources by default and returns a nonzero exit status when that
gate fails. Add `--strict` to include availability-only sources. Tune bounded aggregate requirements with
`--minimum-pass-rate`, `--minimum-fetched-sources`, and `--maximum-fetch-error-rate`. JSON evidence records redirect
chains, response truncation, declared content length, and whether a SHA-256 covers the full body or only its bounded
prefix.
Truncated HTML is classified as `truncated_html` and is not expanded or accepted as behavioral crawl content; increase
`--max-content-bytes` for a bounded recheck instead of treating a script-heavy prefix as a complete page.

## Running the Tests

Every task includes a validated `keyless` contract. Run all 17 live browser contracts without any LLM API key:

```bash
uv run python tests/ci/evaluate_tasks.py \
  --mode deterministic \
	--executable-path /usr/bin/google-chrome \
  --max-parallel 1 \
  --output /tmp/keyless-evaluation.json
```

Use `--disable-sandbox` only on a trusted CI host where Chromium user namespaces are unavailable. Keyless contracts allow only validated
navigation, click, fill, submit, and wait actions; task YAML cannot execute arbitrary browser JavaScript. Results include structured fields,
collections, validator evidence, timings, action traces, and machine-readable failure reasons.
All evaluation modes accept the same browser runtime options: `--executable-path` for a system Chrome, or `--use-cloud-browser` with optional
`--cloud-profile-id`, `--cloud-proxy-country-code`, and `--cloud-timeout-minutes`. A shared browser preflight runs before task subprocesses;
`--skip-browser-preflight` is intended only for the already-validated child process created by the evaluator. Final extraction requires
consecutive stable URL, title, DOM selector count, and visible-text length captures. Tune this with `--required-stable-states`,
`--state-stability-tolerance`, and `--state-retry-delay-seconds` when a source has known delayed rendering.
Each isolated evaluator has a 150-second process deadline and one fresh-process retry; a timeout terminates its Chromium process group instead
of blocking the complete CI lane. Override these bounded defaults with `--task-timeout-seconds` and `--subprocess-attempts` when required.

Available evaluation modes:

- `auto`: resolve to deterministic-first `hybrid` execution. Verified contracts run without an LLM; only failures escalate to an explicitly
  configured local model or an authenticated subscription CLI.
- `hybrid`: the explicit form of `auto`, preserving deterministic failure evidence in any autonomous fallback result.
- `deterministic`: execute the live browser contract without an LLM. This is the required CI lane and requires all 17 tasks to execute and pass.
- `replay`: use native `Agent.rerun_history()` when the task declares a checked-in
  `replay_history_path` inside its task directory. AI-dependent history steps fail explicitly
  because replay never calls an external model. Without saved history, replay the same validated
  declarative actions as a separate regression tier.
- `local`: run the real agent and judge through a user-selected Ollama or OpenAI-compatible local model.
- `subscription`: run the real text-only agent and judge through authenticated Codex, Claude, or Grok CLIs without forwarding API keys.
- `cloud`: run the preferred `ChatBrowserUse` agent and independent Google judge.

### Subscription-authenticated models

For repeatable production browser automation, `ChatBrowserUse` remains the recommended default. The subscription route is an opt-in local
evaluation and development fallback for machines that are already signed in to the official CLIs. Sign in with `codex login`,
`claude auth login`, or `grok login --oauth`; Codex supports ChatGPT subscription authentication as described in the
[official OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth).

Run all 17 scenarios with one model acting and a different model judging:

```bash
uv run python tests/ci/evaluate_tasks.py \
  --mode subscription \
  --subscription-provider codex \
  --subscription-judge-provider claude \
  --max-parallel 1 \
  --task-timeout-seconds 300 \
  --output /tmp/subscription-evaluation.json
```

Valid providers are `codex`, `claude`, and `grok`. `--subscription-model` and `--subscription-judge-model` forward an explicit model name
verbatim; omitting them leaves model selection to the corresponding CLI. The matching environment variables are
`SUBSCRIPTION_LLM_PROVIDER`, `SUBSCRIPTION_LLM_MODEL`, `SUBSCRIPTION_JUDGE_PROVIDER`, and `SUBSCRIPTION_JUDGE_MODEL`.

The adapter reads no credential files, removes OpenAI/Anthropic/xAI API-key variables from child processes, disables coding tools, web
search, persistence, and subagents where each CLI supports those controls, and uses a temporary read-only working directory. It is text-only,
so normal Python usage must configure `Agent(use_vision=False)`. Missing or expired subscriptions are reported as skipped provider routes;
an all-skipped run still fails the quality gate. Do not place personal subscription sessions in shared CI runners.
Codex-specific response schemas omit unsupported JSON Schema `format` annotations during transport and still validate returned data against
the original Pydantic v2 model. Model evaluations pass only when the agent reports completion, produces output without execution errors, and
the full trace judge validates the run; use `history.is_validated()` rather than treating `history.is_successful()` as independent evidence.

Local model names are never replaced. Configure an installed model explicitly:

```bash
KEYLESS_LLM_PROVIDER=ollama \
KEYLESS_LLM_MODEL='<installed-model-name>' \
KEYLESS_LLM_BASE_URL=http://127.0.0.1:11434 \
uv run python tests/ci/evaluate_tasks.py --mode local
```

For an OpenAI-compatible local server, use `KEYLESS_LLM_PROVIDER=openai_like` and a base URL ending in `/v1`. The cloud runner requires both
`BROWSER_USE_API_KEY` for `ChatBrowserUse` and `GOOGLE_API_KEY` for the independent judge. Provider availability is checked once before task
subprocesses are launched; local mode also performs one bounded inference preflight. An all-skipped run fails the quality gate.

When a behavioral source consistently returns a verified anti-bot page, a task may declare a minimal reviewed snapshot containing its review
date, exact source URL, and canonical SHA-256. The report marks this as `snapshot_fallback`; it is never represented as successful live extraction.

---

Happy contributing! 
