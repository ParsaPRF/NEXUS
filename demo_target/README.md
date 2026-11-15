# demo_target/

**Not implemented yet — this is Phase 4.**

This directory will contain the **bundled demonstration registration application**: a small,
self-contained web app owned by this project that implements a realistic sign-up flow:

```
POST /register  ->  sends a verification email  ->  POST /verify  ->  account active
```

It exists so that NEXUS can be developed and demonstrated **without touching anybody else's
system**. It is the default target when no external staging application has been authorized.

## Why it matters

The brief requires that registration testing only ever run against applications the owner
controls or is explicitly authorized to test. Shipping a demo target means the correct default
is also the easy default — you do not need anyone's permission to run the full flow against it.

## Design constraints it must satisfy

| Constraint | Reason |
|---|---|
| Runs locally on a fixed port | No external dependency, no network exposure |
| Sends mail to Mailpit on `127.0.0.1:1025` only | It must never deliver real email |
| Uses its own SQLite database | Total isolation from NEXUS's own data |
| Has a switch to simulate a 429 / CAPTCHA / denial | So the "stop on anti-abuse control" path can be tested honestly |
| Has its own test suite | A demo target with bugs produces meaningless registration results |
| Deliberately contains one configurable slow path | To prove the runner's timeouts work |

That fourth item is the important one: the ability to *simulate* a refusal is what lets us
verify that NEXUS stops and reports instead of retrying around it.

## See also

- [ROADMAP.md](../docs/ROADMAP.md) — Phase 4 task list and exit criteria
- [SECURITY.md](../docs/SECURITY.md) §3 — the ownership rules this target makes testable