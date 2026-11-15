"""Run the demonstration target on a fixed local port.

    uv run python -m demo_target.run

It binds loopback only, and its default mailer submits to Mailpit on loopback. Nothing it does
can reach the public internet.
"""

from __future__ import annotations

import uvicorn


def main() -> None:
    """Start the demonstration application."""
    uvicorn.run("demo_target.app:app", host="127.0.0.1", port=9000, reload=False)


if __name__ == "__main__":
    main()