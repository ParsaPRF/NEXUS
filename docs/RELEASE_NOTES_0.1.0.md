# NEXUS 0.1.0 — release notes

First complete build of the NEXUS platform: backend, desktop client, and the bundled demonstration
target.

**License: Apache License 2.0** — see [LICENSE](../LICENSE). You may use, modify and redistribute
this, including commercially. One third-party obligation applies to the packaged build — see
[Licensing of this build](#licensing-of-this-build) below.

## What is in this release

| Component | Status |
|---|---|
| Backend API (FastAPI, 4 modules, 33 endpoints) | ✅ working, 654 tests |
| Desktop client (PySide6, 7 pages) | ✅ working, 107 tests, packaged as a Windows executable |
| Bundled demonstration registration application | ✅ working |
| Proxy Manager | ✅ import, de-duplicate, check, expire, export |
| Email Manager | ✅ test inboxes, ingestion, code extraction, expiry, export |
| Registration Lab | ✅ campaigns with end-to-end email verification |
| Task Manager & analytics | ✅ durable jobs, cancellation, reports |
| Docker profile (PostgreSQL, Redis/RQ, Mailpit) | ⚠️ written, **not executed** — see "Known gaps" |

## The Windows build

```
NEXUS-0.1.0-win64.zip   (49.5 MB compressed, ~121 MB unpacked, 223 files)
  NEXUS.exe
  START-NEXUS.cmd       double-click launcher (put your API key in it once)
  README-FA.md          Persian quick-start written for the project owner
  LICENSE               Apache-2.0 (this project)
  NOTICE                attribution notices
  THIRD-PARTY-NOTICES.md  every dependency and its licence
  licenses\LGPL-3.0.txt   Qt's licence  ┐ required by LGPL-3.0 §4b when
  licenses\GPL-3.0.txt    GPL-3.0 text  ┘ redistributing a Qt-based build
  _internal\...
```

No Python installation is required on the target machine.

> The Persian guide is shipped **with the application**, not in `docs/`, because the repository's
> documentation is English by project rule ([DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) §11). It is
> written for the person using the tool, not for a developer reading the code.

### Install

1. Extract the archive anywhere writable (for example `D:\NEXUS`).
   Do not run it from inside the zip: Windows would unpack to a temporary folder on every launch.
2. Start the **backend** first — the client is a pure API consumer and has nothing to show without
   it. See `docs/DEPLOYMENT.md` §1.
3. Open `START-NEXUS.cmd` in Notepad once and set `NEXUS_DESKTOP_API_KEY` to your key. After that,
   double-clicking it is all you need.
4. Run `START-NEXUS.cmd` (or `NEXUS.exe`).

### Configure the client

The client reads three environment variables. Set them for your user account so you do not have to
type them every time:

```powershell
[Environment]::SetEnvironmentVariable("NEXUS_DESKTOP_API_URL", "http://127.0.0.1:8000", "User")
[Environment]::SetEnvironmentVariable("NEXUS_DESKTOP_API_KEY", "<your api key>", "User")
```

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_DESKTOP_API_URL` | `http://127.0.0.1:8000` | where the backend listens |
| `NEXUS_DESKTOP_API_KEY` | falls back to `NEXUS_API_KEY` | the `X-API-Key` value |
| `NEXUS_DESKTOP_VERIFY_TLS` | `true` | certificate verification for an https backend |

### Verify the download

```
SHA256: F0CC0C8040E06FA9C4B52CDF4E9A08D42F4FE57756B0E087986C985B8D4C06A1
```

```powershell
Get-FileHash .\NEXUS-0.1.0-win64.zip -Algorithm SHA256   # compare with the .sha256 file
```

### Self-check

The executable can verify itself against a running backend — useful when a machine behaves
differently from the one it was built on:

```powershell
$env:NEXUS_DESKTOP_API_URL = "http://127.0.0.1:8000"
$env:NEXUS_DESKTOP_API_KEY = "<your api key>"
.\NEXUS.exe --self-check report.json
$LASTEXITCODE   # measure with Start-Process, see the note below
```

It writes a JSON report (pages built, navigation, activity, backend readiness) and exits 0 on
success. This is exactly how the shipped build was verified: extracted from this archive, run, and
reported `ok: true` with all 7 pages and live API readiness.

> **PowerShell note:** the call operator does not set `$LASTEXITCODE` for a GUI-subsystem
> executable. To read the code reliably use
> `(Start-Process .\NEXUS.exe -ArgumentList "--self-check","report.json" -Wait -PassThru).ExitCode`

## Licensing of this build

| | |
|---|---|
| NEXUS's own code | **Apache-2.0** — permissive, with an explicit patent grant. Use it, fork it, ship it commercially. |
| Bundled Qt (via PySide6 6.12) | **LGPL-3.0-only**. A redistributor must keep `LICENSE`, `NOTICE`, `THIRD-PARTY-NOTICES.md` and `licenses\` next to the executable, and must not remove the separate Qt DLLs under `_internal\PySide6\`. |
| PyInstaller | GPLv2 **with the bootloader exception**, which permits bundling under any licence. |

The build is *onedir* partly for this reason: it keeps Qt as 13 separate, replaceable DLLs, which is
what LGPL-3.0 §4d requires. Full detail and the reasoning:
[THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) and
[DECISION_LOG.md](DECISION_LOG.md) D23.

If you fork NEXUS and distribute your own build, keep those files. If you need a build without the
LGPL obligations, the answer is a commercial Qt licence — that is a business decision, not something
this repository can decide for you.

## Known gaps (stated, not hidden)

1. **The Docker profile is unverified.** The Docker engine on the build machine does not run, so
   Compose startup, migrations against PostgreSQL, real Mailpit delivery and a real RQ worker have
   not been executed. `tests/e2e/test_real_infrastructure.py` covers them and *skips* with a
   message naming what is missing. To close it: start Docker, `docker compose up -d`, then
   `uv run pytest -m e2e` in `backend/`.
2. **No installer and no code signing.** This is a folder build; Windows SmartScreen will warn on
   first run. Verify the checksum above.
3. **No Playwright UI adapter.** The bundled demo target exposes a documented JSON API and has no
   HTML UI to drive, so a browser adapter would be untested code (`docs/DECISION_LOG.md` D16).
4. **Client exports report counts** rather than opening a save dialog.
5. **No inbound API rate limiting** (outbound limits — campaign rate, concurrency, timeouts — are
   enforced). Recorded as finding F3 in `docs/SECURITY_REVIEW.md`.

## What this software will not do

Registration testing only runs against targets you own or are explicitly authorized to test: a
target needs a written authorization note **and** an allowlist entry (the bundled demo target is
exempt only because it must be local). Anti-abuse responses from a target — 429, CAPTCHA, access
denial — **stop the run** and are reported; nothing retries around them. Inboxes exist only on an
RFC-reserved domain backed by Mailpit, so no third-party mailbox is ever created. Registration has
no proxy setting at all, so the proxy pool cannot be used to disguise traffic.

## Security

`pip-audit` over both dependency sets: no known vulnerabilities. No secret is stored in the
repository or its git history. Full record: `docs/SECURITY_REVIEW.md`.