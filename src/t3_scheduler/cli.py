from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .runner import RunnerError, SchedulerRunner
from .t3 import T3Error


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
    failover = subcommands.add_parser(
        "failover", help="scan once for quota failures and coordinate recovery"
    )
    failover.add_argument("--dry-run", action="store_true")
    failover_status = subcommands.add_parser(
        "failover-status", help="show recent failover incidents"
    )
    failover_status.add_argument("--limit", type=_positive_int, default=50)
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
        if args.command == "failover":
            result = runner.scan_failover(dry_run=args.dry_run)
            return int(
                any(
                    action.outcome in {"manual-review", "exhausted"}
                    for action in result.actions
                )
            )
        if args.command == "failover-status":
            return runner.failover_status(limit=args.limit)
        client = runner.client
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
            identity = client.check_base_dir_identity()
            if identity.compatible is False:
                raise T3Error(
                    "saved scheduler credential does not belong to configured "
                    f"t3.base_dir {identity.configured_base_dir} ({identity.reason})"
                )
            client.ensure_server()
            token = client.token()
            snapshot = client.shell_snapshot()
            roots = {str(project.get("workspaceRoot")) for project in snapshot.get("projects", [])}
            provider_count = None
            if config.failover.enabled:
                providers = client.provider_snapshots(refresh=False)
                provider_ids = {
                    str(provider.get("instanceId"))
                    for provider in providers
                    if isinstance(provider, dict) and provider.get("instanceId")
                }
                configured_ids = {
                    provider.instance_id
                    for provider in config.failover.providers
                    if provider.enabled
                }
                missing = sorted(configured_ids - provider_ids)
                if missing:
                    raise RunnerError(
                        "failover provider instance(s) not found in T3: " + ", ".join(missing)
                    )
                provider_count = len(provider_ids)
            print(f"Configuration OK: {len(config.jobs)} job(s)")
            print(f"T3 server OK: {config.t3.base_url}")
            print(f"T3 auth OK: {len(token)}-character protected credential")
            print(f"T3 projects visible: {len(roots)}")
            if provider_count is not None:
                print(
                    f"Failover {config.failover.mode}: "
                    f"{len(config.failover.providers)} configured; "
                    f"{provider_count} T3 provider instance(s) visible"
                )
            return 0
        raise AssertionError(args.command)
    except (ConfigError, RunnerError, T3Error, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
