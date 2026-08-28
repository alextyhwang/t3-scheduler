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

## Why this exists

T3 is a great place to supervise agent work, but it does not currently expose a cron-style CLI. Running `codex exec` on a timer works, but those runs do not become native T3 threads. T3 Scheduler bridges that gap.

- Native T3 threads, visible alongside interactive work
- Cron schedules with per-job IANA timezones
- Automatic catch-up for briefly missed schedules
- DPAPI-protected T3 credentials with automatic renewal
- SQLite run journal and stable command IDs to avoid duplicate dispatches
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

Finally, install the one-minute scheduler from an elevated terminal:

```powershell
install-task.cmd
```

The desktop app can now stay closed. A lightweight headless T3 server is started only when a due job needs it.

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
| `test.cmd` | Run the unit test suite |

## How it works

```text
Windows Task Scheduler
        |
        v
  due-job runner ----> SQLite run journal
        |
        v
headless T3 server ----> native T3 thread ----> Codex
```

The Windows task wakes once per minute and exits immediately when nothing is due. Successful `(job, scheduled minute)` pairs are not sent twice. If a request has an uncertain result, retries reuse the original T3 command and thread IDs.

## Status and compatibility

T3 Scheduler is Windows-first and currently targets T3 Code's local orchestration API. That API is not yet a documented public compatibility surface, so a future T3 release may require an update here. Run `run.cmd doctor` after upgrading T3.

This is an independent community project and is not affiliated with or endorsed by T3 Code.

## License

MIT. See [LICENSE](LICENSE).
