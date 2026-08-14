# Restart Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent raw process cleanup from killing production permanently and make restart, recovery, and rollback verifiable.

**Architecture:** A foreground supervisor owns the exact child PID and an atomic lock; launchd owns the supervisor. A restart transaction stores stable/candidate revisions independently from the resume message. Runtime health is verified by boot identity rather than process-name matching.

**Tech Stack:** zsh, macOS launchd, Python 3.13, aiohttp, pytest

---

### Task 1: Process-control Hard Guard

**Files:**
- Modify: `vococo/tools/danger.py`
- Modify: `tests/test_danger.py`

- [ ] Add failing tests proving vococo-targeted kill commands are denied and generic process control escalates.
- [ ] Run the focused tests and confirm they fail for the missing rule.
- [ ] Add the minimal classifier/Hard Guard implementation and fail-closed exception handling.
- [ ] Run `tests/test_danger.py` and commit.

### Task 2: Restart transaction state

**Files:**
- Modify: `vococo/tools/selfops.py`
- Create: `tests/test_selfops.py`
- Modify: `vococo/gateway/run.py`

- [ ] Add failing tests for stable/candidate revision storage, global single-flight, supervisor preflight, and rollback metadata surviving resume consumption.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Implement atomic restart transaction files and stable revision tracking.
- [ ] Run focused tests and commit.

### Task 3: Foreground supervisor and exact PID control

**Files:**
- Modify: `deploy/run.sh`
- Modify: `deploy/restart.sh`
- Modify: `deploy/stop.sh`
- Create: `tests/test_deploy_scripts.py`

- [ ] Add subprocess integration tests with a temporary fake serve command.
- [ ] Confirm failures for duplicate supervisor, exact child restart, and stale PID handling.
- [ ] Replace process-name matching with atomic lock and PID files.
- [ ] Run focused integration tests and commit.

### Task 4: Runtime health and doctor

**Files:**
- Modify: `vococo/gateway/adapters/web.py`
- Modify: `vococo/__main__.py`
- Create: `tests/test_runtime_health.py`
- Modify: `deploy/restart.sh`

- [ ] Add failing tests for `/healthz` content and doctor/restart verification by boot_id.
- [ ] Confirm the focused failures.
- [ ] Add the health route and replace `pgrep` success checks.
- [ ] Run focused tests and commit.

### Task 5: LaunchAgent migration and watchdog safety

**Files:**
- Modify: `deploy/launchd.sh`
- Modify: `vococo/gateway/watchdog.py`
- Modify: `tests/test_deploy_scripts.py`
- Create: `tests/test_watchdog.py`

- [ ] Add failing tests for KeepAlive foreground configuration, legacy label removal, and watchdog log-write failure.
- [ ] Confirm focused failures.
- [ ] Implement migration and watchdog fallback.
- [ ] Run focused tests and commit.

### Task 6: Integration and recovery

**Files:**
- Modify: `AGENTS.md`
- Modify: `OPERATIONS.md`
- Modify: `README.md`

- [ ] Run the complete test suite.
- [ ] Merge the branch into main with `deploy/merge-main.sh`.
- [ ] Install the single LaunchAgent and start the foreground supervisor.
- [ ] Verify `/healthz`, changed boot_id after controlled restart, SQLite integrity, one supervisor, and one serve process.
- [ ] Record reusable operational lessons and report exact verification evidence.
