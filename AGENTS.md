# T3 Scheduler agent notes

- Edit `jobs.toml`; keep every `id` unique and use an absolute T3 project path.
- Run `setup.cmd` once after cloning or changing dependencies.
- Preview a thread without sending it: `run.cmd run JOB_ID --dry-run`.
- Kick off a real native T3 thread: `run.cmd run JOB_ID`. It will appear in T3 Code.
- Exercise due jobs: `run.cmd tick --dry-run`, then `run.cmd tick`.
- Run tests after changes: `test.cmd`.
- Run `run.cmd doctor` after changing T3 connection or auth code.
- Never commit `.state/`; it contains the DPAPI-protected T3 credential and run journal.
