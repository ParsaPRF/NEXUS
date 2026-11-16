# NEXUS — Docker recovery protocol

This document exists because one acceptance criterion of the project — *"Docker Compose launches all
required infrastructure"* — could not be verified on the build machine. Below is the **diagnosis**
(what is actually wrong, with evidence), the **repair procedure**, and the **verification** that
closes the item.

Nothing here is guesswork: every statement in §1 was produced by a command whose output is quoted.

---

## 1. Diagnosis: Docker Desktop is installed without its system service

| Check | Command | Result |
|---|---|---|
| Windows service registered? | `Get-Service com.docker*` | **none** — no `com.docker.service` |
| System config directory present? | `Test-Path C:\ProgramData\DockerDesktop` | **False** (also `C:\Program Files\Docker`, `C:\ProgramData\Docker`) |
| Where is it installed? | registry `DisplayName -like '*Docker*'` | **per-user** at `%LOCALAPPDATA%\Programs\DockerDesktop`, version 4.87.0 |
| Virtualization available? | `Win32_ComputerSystem.HypervisorPresent` | **True** — the host is fine |
| WSL2 working? | `wsl -l -v` | yes: `Ubuntu-24.04` (default) and `docker-desktop` both WSL2 |
| Engine answers? | `docker info` | **hangs**, never returns |
| docker-desktop distro contents | `wsl -d docker-desktop -e sh -c "which dockerd"` | **empty**; `/var/run/docker.sock` does not exist |
| Desktop backend alive? | `docker desktop diagnose` | `open \\.\pipe\dockerBackendApiServer: The system cannot find the file specified` |

### What that means

The Linux engine is a `dockerd` inside the `docker-desktop` WSL distribution, started and supervised
by the Windows service `com.docker.service` using config under `C:\ProgramData\DockerDesktop`. On
this machine **neither exists**: the application is installed per-user only, so the privileged half
of Docker Desktop was never installed.

That single fact explains every symptom consistently:

* `docker` CLI answers local commands (it is just a client binary) but every call that reaches the
  daemon hangs;
* the WSL distribution boots but contains no engine to run;
* restarting the application cannot help — there is nothing to start;
* `docker desktop diagnose` finds none of the backend named pipes.

It is **not** a NEXUS problem, not a firewall problem, and not a WSL problem.

---

## 2. Repair

The fix has to be done by an administrator, because it registers a Windows service. It could not be
performed from this session, and it must not be attempted silently: uninstalling and reinstalling a
system component is destructive and needs your consent.

### Step 1 — remove the per-user installation

Windows **Settings → Apps → Installed apps → Docker Desktop → Uninstall**.

Reboot even if not prompted. A leftover per-user install is what caused this state; reinstalling
over it may reproduce it.

### Step 2 — install for all users, elevated

1. Download the current Docker Desktop installer from
   <https://www.docker.com/products/docker-desktop/>.
2. Right-click → **Run as administrator**.
3. On the configuration page, **keep "Use WSL 2 instead of Hyper-V"** enabled.
4. Keep the default install location (`C:\Program Files\Docker\Docker`) — do not install per-user.

> If the installer offers "Install for me only", do not accept it. That choice is what produces
> exactly the state described in §1.

### Step 3 — finish the WSL integration

```powershell
wsl --update
wsl --status
```

### Step 4 — start and wait for the engine

```powershell
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
# wait until Settings shows "Engine running", then:
docker info --format '{{.ServerVersion}} / {{.OSType}}'
```

`docker info` must return a version and `linux`. If it hangs again, run
`docker desktop diagnose` — the report names the failing component.

### Step 5 — confirm the service now exists

```powershell
Get-Service com.docker.service          # should be Running
Test-Path C:\ProgramData\DockerDesktop  # should be True
```

If either is still missing, the installer did not run elevated; repeat Step 2.

---

## 3. Verification (this closes the open item)

```powershell
cd NEXUS
Copy-Item .env.example .env
# set NEXUS_SECRET_KEY, NEXUS_API_KEY, NEXUS_PROXY_CREDENTIAL_KEY in .env

docker compose config --quiet      # syntax only — already known to pass
docker compose up -d
docker compose ps                  # db, redis, mailpit, api, worker all Up
```

Wait for the health checks, then:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready | ConvertTo-Json -Depth 4
```

Expected: `ready: true`, `database: ok`, `job_backend: skipped` (RQ configured), `security: ok`.

Then the real-infrastructure suite — **this is the evidence that was missing**:

```powershell
cd backend
uv run pytest -m e2e -v
```

Expected: 11 passed, in these groups:

| Group | What it proves |
|---|---|
| PostgreSQL | dialect works: connection, schema creation, timezone-aware timestamps |
| Mailpit | real SMTP delivery, its HTTP API, code extraction from a delivered message |
| Redis / RQ | enqueue on real Redis; a worker process executes the dispatcher |
| Full flow | registration verified end to end with **real** mail, on **real** PostgreSQL |

A skipped run is **not** a pass. Only `11 passed` closes the item.

### Then record it

1. In `docs/ROADMAP.md` Phase 7, change item 7.1/7.2 from ❌ to ✅ and paste the `pytest -m e2e`
   summary.
2. In `docs/SECURITY_REVIEW.md` §1, change the real-infrastructure row from ⚠️ to ✅, and update
   finding F1 to resolved.
3. In the README acceptance table, change criterion 2 from ⚠️ to ✅.

---

## 4. If the engine still will not start

| Symptom in the diagnose report | Meaning | Action |
|---|---|---|
| `com.docker.service` still missing | installer was not elevated | uninstall, reinstall **as administrator** |
| `WSL is not installed` / `wsl --status` fails | WSL feature disabled | `wsl --install`, reboot |
| `Virtualization is not enabled` | VT-x/AMD-V off in firmware | enable it in BIOS/UEFI |
| `docker-desktop` distro errors on boot | corrupt distribution | `wsl --unregister docker-desktop`, then restart Docker Desktop (it recreates it) |
| Engine starts, only `Ubuntu-24.04` has no access | WSL integration off | Docker Desktop → Settings → Resources → WSL integration → enable for the distro |

The last command in every case is the same: `docker desktop diagnose` writes a zip of logs that
names the failing component. Read it before changing anything else.

---

## 5. Working without Docker

The `local` profile does not need any of this: SQLite, an in-process job runner and an in-memory
mail backend. Everything in NEXUS except *real* PostgreSQL/Redis/Mailpit behaviour is exercised by
the default test suite (654 tests) on that profile. See `docs/DEPLOYMENT.md` §1.

So the missing item is narrow and precisely stated: **the docker profile is unverified**, not broken.