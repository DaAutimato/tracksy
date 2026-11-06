"""Einstieg: ``python -m zeiterfassung [--hidden] [--no-tray]``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .app import run  # noqa: PLC0415 (GTK erst hier laden)

    return run(sys.argv if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
