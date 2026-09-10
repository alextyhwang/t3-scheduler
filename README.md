<p align="center">
  <img src="assets/t3-scheduler.png" width="180" alt="T3 Scheduler icon: a T3 monogram with a clock built into the number three">
</p>

<h1 align="center">T3 Scheduler</h1>

<p align="center"><strong>Give your T3 agents a clock.</strong></p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/platform-Windows-0078D4?logo=windows11&logoColor=white">
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-8A2BE2">
</p>

T3 Scheduler runs Codex jobs on a schedule and opens each one as a real thread in T3 Code. You get familiar five-field cron expressions, durable run history, and threads that show up in the normal T3 interface—without leaving the desktop app open overnight.

Windows Task Scheduler provides the clock. When a job is due, this project starts T3's bundled server in headless mode if needed, authenticates with limited permissions, and dispatches the prompt through T3's native orchestration API.

The project also contains an opt-in provider failover coordinator. It can detect a terminal usage-limit failure, rank compatible provider instances, and continue the same native T3 thread. Failover is disabled by default and is intended to move through disabled, shadow, and active rollout stages.

## Why this exists

T3 is a great place to supervise agent work, but it does not currently expose a cron-style CLI. Running `codex exec` on a timer works, but those runs do not become native T3 threads. T3 Scheduler bridges that gap.

- Native T3 threads, visible alongside interactive work
- Cron schedules with per-job IANA timezones
- Automatic catch-up for briefly missed schedules
- DPAPI-protected T3 credentials with automatic renewal
- SQLite run journal and stable command IDs to avoid duplicate dispatches
- Durable failover incidents with at-most-once provider attempts
- No custom always-running scheduler daemon

## Quick start

Clone the repository, then run:

```powershell
setup.cmd
```

Setup creates an isolated Python environment and a private `jobs.toml` from the public example. Open `jobs.toml`, point the example at a project already known to T3, add your prompt, and set `enabled = true`.

Check the connection and preview the exact thread command:

```powershell
run.cmd doctor
run.cmd run example-nightly-review --dry-run
```

Send it immediately when the preview looks right:

```powershell
run.cmd run example-nightly-review
```

Finally, install the one-minute scheduler:

```powershell
install-task.cmd
```

The desktop app can now stay closed. A lightweight headless T3 server is started only when a due job needs it.

An elevated install uses an S4U task with highest privileges and can run while
the user is signed out. A non-elevated install creates a limited current-user
task that runs while that user is signed in.

## Define a job

Schedules use `minute hour day-of-month month day-of-week`. Wildcards, lists, ranges, and steps are supported. Sunday is `0` or `7`.

```toml
[[jobs]]
id = "nightly-tests"
enabled = true
cron = "15 20 * * 1-5"
timezone = "America/Los_Angeles"
project = "C:\\Users\\you\\Projects\\your-project"
title = "Nightly test investigation"
prompt_file = "prompts/private/nightly-tests.md"
instance_id = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
service_tier = "default"
runtime_mode = "full-access"
interaction_mode = "default"
misfire_policy = "run-once"
```

Use either `prompt` for a short inline prompt or `prompt_file` for a longer one. Prompts can include `{{job_id}}` and `{{scheduled_for}}`.

`run-once` catches the most recent missed occurrence within `scheduler.misfire_grace_minutes`. Use `skip` when a late run would be worse than no run.

## Configure provider failover

Failover uses T3's configured provider instances; it does not copy or merge account credentials. Every candidate must be enabled, installed, authenticated, available, use the same provider driver and continuation group as the current thread, and support the thread's model.

Start in shadow mode:

```toml
[failover]
enabled = true
mode = "shadow"
max_usage_age_seconds = 300
allow_unknown_usage = false

# Interactive chats are excluded until this is deliberately enabled.
allow_interactive_threads = false

# Empty means every project within the selected scheduled/interactive scope.
# Relative paths are resolved from jobs.toml.
project_allowlist = [
  "C:\\Users\\you\\Projects\\important-project",
]

[[failover.providers]]
instance_id = "codex_account_2"
priority = 100
enabled = true

[[failover.providers]]
instance_id = "codex_account_3"
priority = 100
enabled = true

[[failover.providers]]
instance_id = "codex_jimmy"
priority = 50
enabled = true

[[failover.providers]]
instance_id = "codex_oliver"
priority = 50
enabled = true
```

`enabled` is the master switch. The rollout modes are:

- **Disabled:** set `enabled = false`; the coordinator does no failover work.
- **Shadow:** detects incidents and calculates what it would do, without sending a continuation turn.
- **Active:** may send a continuation turn after all safety and compatibility checks pass.

Higher priority values form preferred tiers. Within the highest eligible tier, the coordinator chooses the account with the most effective headroom. Effective headroom is the smallest remaining percentage across its active usage windows, so an exhausted weekly allowance cannot be hidden by a healthy short window. Configuration order breaks exact ties.

Usage older than `max_usage_age_seconds` is unknown. With `allow_unknown_usage = false`, unknown accounts are excluded. When enabled, unknown accounts rank below accounts with known positive headroom in the same priority tier. An account with a known exhausted active window is never selected.

Scheduled threads are the initial rollout scope. Set `allow_interactive_threads = true` only when failover should also watch chats started manually in T3 Code. Use `project_allowlist` to constrain either scope to known workspace roots before broadening it; an empty list means no project restriction.

### Coordinator safety

The coordinator reacts only to T3's normalized terminal usage-limit failure, not ordinary agent errors, policy refusals, authentication problems, network failures, cancellations, or interruptions. It attempts each eligible provider at most once per incident and preserves the original thread, model, runtime mode, and interaction mode.

Before continuing, it re-reads the thread. If the user has retried, switched providers, or otherwise changed the latest turn, automatic recovery stops. A failed turn with unresolved tool calls, pending approval or user input, or an external write whose outcome cannot be established is placed in manual review rather than replayed. This prevents quota recovery from duplicating side effects.

T3 Nightly may project a persisted user message with no `turnId`. Verification
therefore correlates the coordinator's exact durable message ID and requires it
to remain the latest user message, while still requiring the selected provider
and expected terminal/running state. A conflicting non-null turn ID or a later
user message remains a manual-review condition.

The current T3 continuation command does not expose an atomic expected-turn
precondition. The coordinator performs detail and shell preflight reads as close
as possible to dispatch, but a small read-to-dispatch race remains. Keep active
mode restricted to a project allowlist during the pilot; shadow is the default.

The scheduler's one-minute Windows task provides the coordinator clock: each invocation scans, handles claimable incidents, and exits. Recovery therefore is not instantaneous and can take about one scan interval. If the task is not installed or running, no automatic scan occurs; this repository does not install or activate failover merely because it is configured.

See [the native provider failover plan](docs/provider-failover-plan.md) for the
state machine, safety boundary, rollout stages, release gates, and rollback.

## Keep your automations private

The public repository contains only `jobs.example.toml` and a harmless example prompt. Your actual schedules and automation content stay local:

- `jobs.toml` and `jobs.*.toml`
- `automations/`
- `prompts/private/` and `prompts/*.local.md`
- `.state/`, which holds the encrypted credential and run journal
- `.env*`, `*.secret`, and `*.token`

All of these paths are covered by `.gitignore`. Put personal prompts under `prompts/private/` and they cannot be added accidentally with a normal `git add`.

## Commands

| Command | What it does |
| --- | --- |
| `run.cmd list` | List configured jobs |
| `run.cmd doctor` | Validate configuration, authentication, and T3 connectivity |
| `run.cmd auth` | Renew the protected T3 credential |
| `run.cmd tick --dry-run` | Preview every job due now |
| `run.cmd tick` | Dispatch every job due now |
| `run.cmd run JOB_ID --dry-run` | Preview one job immediately |
| `run.cmd run JOB_ID` | Dispatch one job immediately |
| `run.cmd failover --dry-run` | Preview one failover scan without recording an incident or dispatching a continuation |
| `run.cmd failover` | Run one shadow or active failover scan, according to configuration |
| `run.cmd failover-status` | Show durable failover incident and provider-attempt status |
| `test.cmd` | Run the unit test suite |

## How it works

```text
                    Windows Task Scheduler
                              |
                    one-minute tick, then exit
                         /             \
                        v               v
               due-job runner     failover scanner
                     |                  |
                     v                  v
              SQLite run journal  incident/attempt journal
                         \             /
                          v           v
                   headless T3 server
                           |
                           v
                native T3 thread ----> Codex
```

The Windows task wakes once per minute and exits immediately when there is no work. Successful `(job, scheduled minute)` pairs are not sent twice. If a request has an uncertain result, retries reuse the original T3 command and thread IDs. Failover uses separate durable incident and provider-attempt records so scheduled dispatch idempotency and continuation idempotency cannot interfere with each other.

## Status and compatibility

T3 Scheduler is Windows-first and currently targets T3 Code's local orchestration and provider-status interfaces. These are not yet documented public compatibility surfaces, so a future T3 release may require an update here. Failover must stop safely when required fields or continuation capabilities are absent. Run `run.cmd doctor` after upgrading T3 and verify that `t3.base_dir` points at the active T3 control root; the example uses `%USERPROFILE%\.t3`.

Current T3 HTTP builds validate `bootstrap.createThread` but do not execute that
bootstrap stage. For scheduler-created threads, the client therefore sends a
deterministic `thread.create` command followed by the persisted
`thread.turn.start` command. Both command IDs remain stable across uncertain
retries. Worktree/setup-script bootstrap is rejected because reproducing those
WebSocket-only stages without their lifecycle fences would be unsafe.

Only configure accounts and provider instances you are authorized to use. Automatic failover is not intended to evade provider restrictions, bypass protective measures, or retry a policy refusal. Review the terms for every configured provider, including [OpenAI's Terms of Use](https://openai.com/policies/terms-of-use/), before enabling active mode.

This is an independent community project and is not affiliated with or endorsed by T3 Code.

## License

MIT. See [LICENSE](LICENSE).
