"""Compatibility entry point for the CLI inbound adapter."""

from .interfaces.cli import build_parser, main, run_command

__all__ = ["build_parser", "main", "run_command"]


if __name__ == "__main__":
    raise SystemExit(main())
