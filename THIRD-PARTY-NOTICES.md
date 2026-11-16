# Third-party notices

NEXUS is licensed under the Apache License 2.0 (see [LICENSE](LICENSE)). It depends on, and in its
packaged Windows build **redistributes**, the third-party components listed here.

This file exists because a binary distribution has obligations that a source distribution does not.
Everything below was produced by auditing the resolved dependency sets, not from documentation:

```powershell
cd backend   ; uv run --with pip-licenses pip-licenses --format=markdown --order=license
cd desktop   ; uv run --with pip-licenses pip-licenses --format=markdown --order=license
```

---

## 1. Components that require action when you redistribute a build

### Qt for Python (PySide6) and Qt 6 — LGPL-3.0-only

**This is the one component with real obligations attached, and they apply to the packaged
`NEXUS.exe` build, not to the source code.**

| | |
|---|---|
| Package | `PySide6` 6.12.0, `PySide6_Essentials`, `PySide6_Addons`, `PySide6_Pdf`, `shiboken6` |
| Underlying library | Qt 6.12.0 (see `PySide6.QtCore.qVersion()`) |
| Copyright | The Qt Company Ltd. and contributors |
| License | `LGPL-3.0-only` (dual-offered by Qt as GPL-2.0 / GPL-3.0; NEXUS uses the LGPL option) |
| License text | [`licenses/LGPL-3.0.txt`](licenses/LGPL-3.0.txt), plus [`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt) which LGPL-3.0 incorporates by reference |

**How this project complies.** LGPL-3.0 §4 permits distributing a Combined Work under terms of your
choosing provided that it does not restrict modification of the library, and that you:

| Requirement (LGPL-3.0 §4) | How NEXUS satisfies it |
|---|---|
| (a) prominent notice that the library is used and covered by this license | this file, shipped inside the build, and [NOTICE](NOTICE) |
| (b) accompany the work with a copy of the GNU GPL and the LGPL | `licenses/GPL-3.0.txt` and `licenses/LGPL-3.0.txt` are shipped in the build |
| (c) for a work that displays copyright notices at run time, include the library's notice | NEXUS displays no copyright notices at run time; the notices travel as files instead |
| (d) allow the user to relink with a modified library | the build is **onedir**: `PySide6/Qt6Core.dll`, `Qt6Widgets.dll`, `Qt6Gui.dll`, `Qt6Network.dll` and `shiboken6` are separate files under `_internal\`, so they can be replaced with modified builds. A *onefile* build would make this far harder, which is one reason onedir was chosen ([DECISION_LOG.md](docs/DECISION_LOG.md) D22) |
| (e) Installation Information, only if required by GPL §6 | applies to consumer products with locked firmware; a plain Windows folder does not trigger it |

**What a redistributor must not do:** strip `licenses/`, `NOTICE` or this file from the build, or
merge Qt into a single binary in a way that prevents replacing it.

If these obligations are inconvenient — for example you want to ship a locked-down or store-signed
build — the alternative is a commercial Qt license from The Qt Company. That is a business decision,
not a technical one.

---

## 2. Build tools (not part of what you redistribute, but worth knowing)

### PyInstaller — GPLv2 **with the bootloader exception**

| | |
|---|---|
| Version | 6.22.3 |
| License | GNU General Public License v2, **with a special exception** |

PyInstaller's licence explicitly permits using it to package and distribute applications **under any
license**, including proprietary ones. The GPL applies to PyInstaller itself and to its bootloader,
not to the application you bundle. NEXUS relies on that exception.

`pyinstaller-hooks-contrib` (2026.10) is dual-licensed `Apache-2.0 OR GPLv2`; the Apache-2.0 option
applies.

---

## 3. Permissive dependencies (no obligations beyond attribution)

These are all MIT / BSD / ISC / Apache-2.0 / MPL-2.0 / PSF-2.0 family. Attribution is satisfied by
this notice; none of them impose copyleft on NEXUS's own code.

### Backend — runtime

| Package | License |
|---|---|
| fastapi, starlette (BSD-3-Clause), pydantic, pydantic-settings, pydantic_core | MIT / BSD-3-Clause |
| uvicorn, httptools, watchfiles, websockets, h11 | BSD-3-Clause / MIT |
| SQLAlchemy, Alembic, Mako | MIT |
| aiosqlite, httpx, httpcore, anyio, certifi (MPL-2.0), idna | MIT / BSD-3-Clause / MPL-2.0 |
| argon2-cffi, argon2-cffi-bindings, cffi (MIT-0) | MIT / MIT-0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| python-multipart, python-dotenv, socksio, greenlet (MIT AND PSF-2.0) | Apache-2.0 / BSD-3-Clause / MIT |
| asyncpg (optional, PostgreSQL) | Apache-2.0 |
| redis, rq (optional, queue) | MIT / Apache-2.0 |

### Backend — development only

pytest, pytest-asyncio, pytest-cov, pluggy, iniconfig, coverage, ruff, fakeredis — MIT / BSD-3-Clause.
**Not shipped** in the application.

### Desktop — runtime

| Package | License |
|---|---|
| PySide6, shiboken6 | LGPL-3.0-only (see §1) |
| httpx, httpcore, h11, anyio, certifi, idna, sniffio | MIT / BSD-3-Clause / MPL-2.0 |

### Desktop — development only

PyInstaller (GPLv2 + exception, §2), pytest, pytest-qt, ruff — **not** part of the application's own
code, though PyInstaller's bootloader is embedded in `NEXUS.exe`.

---

## 4. Data and content

NEXUS ships **no** third-party data sets, icons, fonts or media. The Qt modules used here are
functional only; the application does not use Qt's icon theme, so no third-party icon licences
apply. The desktop theme is defined in `desktop/app/theme.py` by this project.

---

## 5. Proxies, mail and registration targets

NEXUS contains no affiliation with, and no code or data from, any proxy provider, mail provider or
registration target. It talks to endpoints the operator configures and owns; see
[docs/SECURITY.md](docs/SECURITY.md).

---

## 6. Keeping this file honest

When a dependency is added or upgraded, re-run the two `pip-licenses` commands at the top and update
this file. A dependency that is GPL or AGPL (rather than LGPL) would change what NEXUS may be
licensed under, so it must be a deliberate decision, recorded in
[docs/DECISION_LOG.md](docs/DECISION_LOG.md) — not a side effect of `uv add`.

*Last audited: 2026-11-16, for release 0.1.0.*
