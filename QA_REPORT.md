# Pawgram Production Readiness / QA Report

Date: 2026-07-28
Version reviewed: 0.3.0
Workspace: `C:\Users\Ömer\Desktop\Tel`

## Executive result

The application source, Telegram execution layer, task lifecycle, proxy handling,
updater, local launcher, licensing service, backup flow, UI/API integration, build
configuration, dependencies, and automated tests were audited and repaired.

Current verification result:

- Ruff: passed.
- Mypy: passed for 27 source files.
- Bandit: no medium/high failures.
- Unit/API tests: 65/65 passed.
- JavaScript syntax: passed.
- Python bytecode compilation: passed.
- `pip check`: no broken requirements.
- `pip-audit`: no known vulnerabilities in the pinned runtime/build requirements.
- PowerShell parser validation: public and commercial build scripts passed.
- Clean PyInstaller QA build: completed successfully.
- Built `Pawgram.exe` startup and `/api/health`: HTTP 200.
- License server smoke test: health, license creation, activation, and validation passed.
- UI/API smoke: index, CSS/JS assets, admin setup/login/logout, protected routes,
  security headers, backup creation/download/content, and backup listing passed.

## Issues found and fixes

### Telegram discovery and member transfer

- Hidden member lists caused scans to return only administrators/bots or a very small
  subset. Message-author scanning was added as a supported fallback and merged with
  participant results.
- Administrators, bots, deleted accounts, existing target members, and previously used
  members are consistently excluded from eligible candidates.
- `UserIdInvalidError` was caused by insufficient Telegram entity context. Candidate
  records now preserve access hashes and source message context, and the executor
  refreshes minimal users through the originating message when necessary.
- Direct member addition now uses `InviteToChannelRequest` for channels/megagroups and
  `AddChatUserRequest` for basic groups.
- Target invite permission is checked before preview/execution.
- Privacy restrictions, already-participant responses, missing entities, and Telegram
  rate limits are recorded per candidate without unnecessarily blocking the remaining
  safe work.
- Proxy/session connection failures no longer consume candidates.

### Job, scan, and scheduler lifecycle

- Activity scans and invite jobs are registered as background tasks instead of blocking
  button requests.
- Duplicate scan execution is rejected, pause performs a real cancellation, resume is
  limited to paused scans, and shutdown safely cancels owned tasks.
- Interrupted scans/jobs are recovered into safe states at startup.
- Candidate selection and approval are restricted to the `previewed` state; completed or
  progressed jobs cannot regress to preview/selection states.
- Scheduled starts, working-hour windows, daily quota resumes, batch cooldowns, FloodWait,
  and PeerFlood resumes persist in `resume_at` and are picked up by the scheduler.
- Cross-midnight working windows and timezone-aware timestamps are handled.

### Proxy safety

- The Telegram login flow previously requested the login code before a proxy was fixed
  to the account, which could expose the server IP. Login now requires and tests a proxy
  before constructing the Telegram client.
- Login request, 2FA verification, and later account operations use the same stored,
  encrypted proxy configuration.
- Proxy failure is fail-closed: Telegram clients are not constructed and the main IP is
  never used as fallback.
- Proxy type auto-detection, latency, last result, and error details are persisted.
- Bulk TXT import assigns one proxy only to each account with an empty proxy slot.
- Save/test/delete proxy UI flows now report progress and actionable errors.
- Natural low-volume batches (three successes followed by a 30-minute cooldown), daily
  quotas, and 24-hour PeerFlood resting are enforced.

### Authentication, security, and configuration

- Admin login and license activation/login endpoints received bounded, thread-safe rate
  limiting.
- Admin password creation is atomic and cannot be overwritten by a setup race.
- Trusted hosts and browser security headers were added.
- SQLite connections use a busy timeout and safer transaction handling.
- App/license server numeric configuration values now have explicit validation bounds.
- A cached module-level settings object caused environment/database paths to become stale
  across isolated runs. Runtime endpoints now read the current cached settings instance,
  allowing intentional cache refreshes to take effect consistently.
- Ed25519 signing and verification reject unexpected key types.

### Licensing service

- Concurrent activations could race beyond `max_devices`. Activation now uses an
  immediate transaction, rollback, and explicit integrity-error handling.
- Weak/empty admin keys are rejected at startup.
- License database failures correctly roll back and propagate.
- Admin cookies are HttpOnly/SameSite; production deployments can require secure cookies.

### Updater and launcher

- The launcher has a cross-platform single-instance lock. A second launch opens the
  running local panel instead of starting a competing server.
- Update archive extraction rejects path traversal, unsafe roots, encrypted members,
  symbolic links, excessive member count/size, and suspicious compression ratios.
- Update manifests require valid semantic versions, SHA-256, and Ed25519 signatures.
- The updater waits for a health-marker handshake and process liveness before deleting
  the rollback backup; failed startup restores the previous installation.
- Cleanup targets are resolved and constrained to the temporary update directory.

### Backup and release engineering

- Database-only backups were not sufficient to restore encrypted session/proxy data.
  Backup ZIPs now include `console.db`, `.secret_key` when present, optional
  `installation_id`, and `backup-info.json` after a SQLite quick check.
- Legacy `.db` backups remain listable/downloadable.
- CI now runs lint, type checking, security analysis, JS syntax, tests, dependency audit,
  and PyInstaller.
- Build scripts create fresh temporary virtual environments from pinned runtime/build
  dependencies rather than reusing stale workspace packages.
- Requirements were pinned and split into runtime, build, and development sets.
- Documentation was aligned with the current 0.3.0 capabilities.

## Files modified and why

| File | Purpose of change |
| --- | --- |
| `.env.example` | Removed obsolete/unsafe configuration surface. |
| `.github/workflows/tests.yml` | Added complete CI quality and build gates. |
| `.gitignore` | Ignored local security/build artifacts. |
| `README.md` | Updated version, implemented features, proxy/invite behavior, and future work. |
| `app/activity_service.py` | Safe task registry, deduplication, cancellation, recovery, and session rotation. |
| `app/config.py` | Bounded validated configuration and cleaner path handling. |
| `app/database.py` | Migrations for scheduling/pending proxy auth, busy timeout, and safer persistence. |
| `app/licensing.py` | Safer device identity fallback and licensing behavior. |
| `app/main.py` | Scheduler/task orchestration, state guards, proxy login API, secure backups, auth/rate limiting, and dynamic settings access. |
| `app/rate_limit.py` | New bounded thread-safe in-memory rate limiter. |
| `app/scheduling.py` | New timezone-aware scheduling and working-window implementation. |
| `app/schemas.py` | Validation models for login proxies, bulk proxies, rotation, and stricter inputs. |
| `app/security.py` | Stronger secret handling and encryption/auth validation. |
| `app/telegram_service.py` | Hidden-member discovery, entity resolution, direct add execution, proxy fail-closed behavior, quotas/cooldowns, and error classification. |
| `app/updater.py` | Signed update validation, archive hardening, health handshake, cleanup safety, and rollback. |
| `license_server/config.py` | Validated server port, lease duration, and cookie/security options. |
| `license_server/database.py` | Transaction timeout/rollback improvements. |
| `license_server/generate_keys.py` | Safer key generation output handling. |
| `license_server/main.py` | Strong key enforcement, rate limits, secure admin sessions, and concurrency-safe activation. |
| `license_server/run_server.py` | Removed obsolete configuration behavior. |
| `license_server/signing.py` | Ed25519 key-type enforcement and signing validation. |
| `requirements.txt` | Pinned runtime dependencies. |
| `requirements-build.txt` | Pinned build dependencies. |
| `requirements-dev.txt` | Added pinned QA/development tooling. |
| `run.py` | Single-instance launcher, existing-panel discovery, cross-platform lock, and safe startup boundary. |
| `scripts/build_commercial.ps1` | Fresh isolated commercial build environment. |
| `scripts/build_public_release.ps1` | Fresh isolated public build, signed manifest creation, and clean-source checks. |
| `scripts/generate_icon.py` | Removed stale/unused code. |
| `scripts/generate_manual.py` | Removed stale/unused code. |
| `scripts/generate_sales_poster.py` | Minor correctness/cleanup adjustment. |
| `scripts/sign_update_manifest.py` | Semantic/version/key validation for signed manifests. |
| `static/app.js` | Non-blocking progress UI, stable modal navigation, job polling, proxy controls, scheduling states, and selection locking. |
| `static/index.html` | Required login-proxy fields, proxy deletion/bulk import controls, and clearer backup/invite UI. |
| `tests/test_activity_safety.py` | Scan deduplication/cancellation, round-robin/quota, and access error coverage. |
| `tests/test_api.py` | API state guards, scheduling, backups, proxies, activity transfer, auth, and license-lock coverage. |
| `tests/test_invite_executor.py` | Direct-add, entity refresh, proxy failure, privacy, PeerFlood, cooldown, and session error coverage. |
| `tests/test_launcher.py` | New single-instance lock coverage. |
| `tests/test_license_server.py` | Rate limits, cookies, lifecycle, and concurrent device activation coverage. |
| `tests/test_login_proxy.py` | New proof that Telegram login is never started before a tested proxy. |
| `tests/test_rate_limit.py` | New rate-limiter bounds/reset coverage. |
| `tests/test_scheduling.py` | New UTC/local/cross-midnight scheduling coverage. |
| `tests/test_security.py` | Expanded encryption/auth/device identifier coverage. |
| `tests/test_updater.py` | Signature, traversal, zip-bomb, symlink, health handshake, and rollback ordering coverage. |

The pre-existing untracked `.video_review/` directory was intentionally left untouched.

## Builds and tests performed

### Automated checks

```text
ruff check --no-cache app license_server scripts tests run.py       PASS
mypy app license_server scripts run.py --ignore-missing-imports     PASS (27 files)
bandit -r app license_server scripts run.py -q -ll                  PASS
unittest discover -s tests -v                                      PASS (65/65)
pip-audit requirements.txt + requirements-build.txt                PASS
node --check static/app.js                                         PASS
compileall app license_server scripts tests run.py                  PASS
pip check                                                          PASS
git diff --check                                                   PASS (line-ending notices only)
PowerShell parser: build_public_release.ps1                         PASS
PowerShell parser: build_commercial.ps1                             PASS
```

### Build/runtime checks

- Clean virtual environment: `%TEMP%\Pawgram-Clean-20260728`.
- PyInstaller QA build to isolated `dist-qa`/`build-qa`: successful.
- Built `Pawgram.exe` startup with isolated port: successful.
- Built application `/api/health`: HTTP 200, build `0.3.0-production`.
- Source launcher startup and health check on an isolated port: successful.
- Generated QA build/output directories were removed after verification.

### Functional smoke checks

- Index, JavaScript, and CSS assets: HTTP 200.
- Admin setup, login, logout, cookie state, and protected API behavior: passed.
- Security response headers: passed.
- Backup creation, ZIP content, download, and listing: passed.
- License server: startup, health, admin license creation, activation, and validation: passed.
- Telegram direct-add behavior is covered with mocked Telethon entities/requests; no live
  mass-add test was performed against real Telegram users.

## Remaining limitations

- Telegram can independently reject additions because of target permissions, account
  trust, privacy settings, hidden members, FloodWait/PeerFlood, user/channel limits, or
  platform policy. These outcomes are now surfaced and persisted but cannot be removed by
  local code.
- Live end-to-end Telegram addition was intentionally not performed against real users;
  request construction and state transitions are covered by deterministic Telethon mocks.
- Visual browser-driver access was unavailable in the final environment. Equivalent
  index/static/auth/navigation-supporting API flows were verified through the real ASGI
  application with persistent TestClient cookies.
- The customer release ZIP/GitHub Release was not produced during this pass, as requested.
- No commit or push was performed. The release build script correctly requires a committed,
  clean source tree before producing signed release assets.

## Recommended future improvements

1. Add a CI browser E2E job for the onboarding, proxy, activity, candidate approval, and
   scheduled execution screens.
2. Code-sign `Pawgram.exe` with a trusted Windows certificate to reduce SmartScreen warnings.
3. Add automated upgrade tests starting from archived legacy database schemas.
4. Add configurable backup retention/restore UI with explicit encrypted-data warnings.
5. For large hosted deployments, move job claiming to PostgreSQL/Redis and add structured
   operational metrics.

## Release readiness conclusion

The reviewed source is buildable and its available automated/runtime checks pass. No known
build error, test failure, dependency conflict, or reproducible application/runtime defect
remains from this QA pass. Release packaging should be performed only after these changes
are reviewed, committed, and the intended next version is selected.
