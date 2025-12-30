"""Deprecated legacy script.

This file previously contained ad-hoc timer test code that executed network
calls on import/run. The project now supports timer control via:

- Web UI: /timers and /api/timers/apply
- CLI: python cli.py timers ...

This module is intentionally kept as a stub to avoid unexpected side-effects.
"""


def main() -> int:
    print("This legacy script is deprecated.")
    print("Use the Web UI at /timers, or run: python cli.py timers --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())