# Native provider failover plan

## Goal

When a Codex turn ends because its configured T3 provider instance has exhausted
usage, continue the same native T3 thread on the best eligible authorized
instance. The operator controls eligibility and priority in `jobs.toml`.

## Selection policy

1. Consider only configured, enabled provider instances.
2. Require the same driver and T3 continuation group as the failed instance.
3. Require an authenticated, installed, available instance that supports the
   thread's current model.
4. Exclude the failed instance and every instance already attempted for the
   incident.
5. Select the highest configured priority tier.
6. Within that tier, select the largest effective headroom, defined as the
   minimum remaining percentage across active usage windows.
7. Use configuration order as the final deterministic tie-break.

## Recovery state machine

```text
detected -> dispatching -> verifying -> recovered
    |           |              |
    |           |              +-> detected (target also exhausted)
    |           +-> retry the identical persisted command after uncertainty
    +-> exhausted | manual-review
```

Each incident is durable and unique for the original failed thread/turn. Each
target instance can be attempted at most once. The continuation command is
stored before dispatch so a crash or uncertain HTTP result reuses the same T3
`commandId` rather than creating another turn.

## Safety boundary

Automatic failover requires a terminal Codex usage-limit error, no active turn,
no pending approval or user input, and no unmatched tool start. The coordinator
re-reads the thread immediately before dispatch. User activity, a manual provider
change, an incompatible model/provider, an invalid T3 response, or an ambiguous
tool/external-write outcome moves the incident to manual review.

The current T3 command has no atomic expected-turn/version precondition. The
coordinator therefore performs both detail and shell preflight reads immediately
before dispatch, but a small read-to-dispatch race remains. Keep active mode
restricted to the documented pilot until T3 exposes an atomic compare-and-start
contract; shadow mode remains the default.

The recovery is a new turn in the same T3 thread. It preserves the model,
options, runtime mode, and interaction mode; changes only the provider instance;
omits thread bootstrap; and instructs the resumed agent to reconcile uncertain
effects before continuing.

## Rollout

1. **Foundation — implemented:** configuration, ranking, T3 provider snapshots,
   thread detail reads, durable incidents/attempts, coordinator scan, CLI, and
   targeted unit and crash-boundary tests are in place.
2. **Live shadow — validated on Blackbox:** enable shadow mode and run
   `run.cmd failover --dry-run` or the scheduler tick. Continue confirming that
   selected instances match manual decisions across T3 Nightly upgrades. No
   continuation is dispatched.
3. **Native transport pilot — complete on Blackbox:** a scheduler-created T3
   thread completed successfully and the same thread continued across multiple
   authorized Codex instances. The HTTP bootstrap compatibility workaround is
   covered by deterministic retry tests.
4. **Active Blackbox rollout — validated:** `allow_interactive_threads` is
   enabled and the authorized fallback instances share one priority tier, so
   remaining quota decides the target. On 2026-09-10, a natural terminal quota
   event switched the same live thread from Alvin to Oliver and the Oliver turn
   completed.
5. **Hardening follow-up:** the first natural event exposed that T3 Nightly
   projects persisted user messages with `turnId: null`. The verifier now
   correlates the exact persisted message ID while rejecting a conflicting turn
   ID or a later user message. Keep manual-review alerts enabled and retain this
   live projection shape in regression tests.
6. **Lower-latency follow-up:** replace one-minute polling with the verified T3
   configuration/thread event streams only if the stable polling release is
   reliable across Nightly upgrades.

## Release gates

- `run.cmd doctor` succeeds against the active T3 base directory and sees every
  configured provider instance.
- The unit suite and targeted crash-boundary tests pass; add a fake-server
  transport/race suite before unattended active rollout.
- A live shadow scan selects the expected account without creating journal rows
  in dry-run mode.
- A live same-thread cross-provider continuation succeeds; verify that the first
  natural quota-triggered recovery produces exactly one continuation turn.
- A second exhausted account advances to an unattempted account and never loops.
- Any T3 contract mismatch disables recovery rather than guessing or editing T3
  databases.
- Logs and journals contain no bearer tokens, credentials, account email
  addresses, or raw provider payloads.

## First natural quota incident

The 2026-09-10 Blackbox acceptance event detected an exhausted Alvin turn and
selected Oliver from fresh provider usage. A manually submitted `continue`
raced the one-minute scan and produced a second Alvin quota error before the
coordinator message was dispatched. The coordinator's following turn ran and
completed on Oliver.

The switch initially appeared as `manual-review / unexpected_thread_change` in
the scheduler journal because live T3 left the coordinator's persisted user
message unbound (`turnId: null`) while the fixture expected the new turn ID.
After adding the live-shape regression, the verifier was fixed and the incident
was reconciled to `recovered`. No T3 thread database was edited.

## Rollback

Set `failover.enabled = false`. The database migration is additive; disabling
the coordinator leaves existing T3 threads, provider settings, scheduled-job
history, and protected credentials untouched.
