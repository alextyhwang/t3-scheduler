from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config
from .runner import RunnerError, SchedulerRunner
from .t3 import T3Client, T3Error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule native T3 Code threads")
    parser.add_argument("--config", type=Path, default=Path("jobs.toml"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    tick = subcommands.add_parser("tick", help="dispatch jobs due now")
    tick.add_argument("--dry-run", action="store_true")
    run = subcommands.add_parser("run", help="dispatch one job immediately")
    run.add_argument("job_id")
    run.add_argument("--dry-run", action="store_true")
    subcommands.add_parser("list", help="list configured jobs")
    subcommands.add_parser("doctor", help="validate configuration and T3 connectivity")
    subcommands.add_parser("auth", help="create or renew the scheduler's T3 credential")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        config = load_config(args.config)
        runner = SchedulerRunner(config)
        if args.command == "list":
            for job in config.jobs:
                state = "enabled" if job.enabled else "disabled"
                print(
                    f"{job.id}: {state}; {job.cron.expression} {job.timezone_name}; {job.project}"
                )
            return 0
        if args.command == "tick":
            return runner.tick(dry_run=args.dry_run)
        if args.command == "run":
            return runner.run_manual(args.job_id, dry_run=args.dry_run)
        client = T3Client(config.t3, config.state_dir)
        if args.command == "auth":
            client.ensure_server()
            client.token(force_renew=True)
            print(f"T3 credential created: {client.credential_path}")
            return 0
        if args.command == "doctor":
            for job in config.jobs:
                if not job.project.is_dir():
                    raise RunnerError(f"job {job.id!r} project does not exist: {job.project}")
                if job.prompt_file and not job.prompt_file.is_file():
                    raise RunnerError(f"job {job.id!r} prompt file does not exist: {job.prompt_file}")
            client.ensure_server()
            token = client.token()
            snapshot = client.shell_snapshot()
            roots = {str(project.get("workspaceRoot")) for project in snapshot.get("projects", [])}
            print(f"Configuration OK: {len(config.jobs)} job(s)")
            print(f"T3 server OK: {config.t3.base_url}")
            print(f"T3 auth OK: {len(token)}-character protected credential")
            print(f"T3 projects visible: {len(roots)}")
            return 0
        raise AssertionError(args.command)
    except (ConfigError, RunnerError, T3Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
