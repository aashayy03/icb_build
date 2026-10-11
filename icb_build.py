#!/usr/bin/env python3
"""ICB build orchestrator entry point. Run with --help for usage."""

import sys

if sys.version_info < (3, 8):
    sys.exit("icb_build requires Python 3.8 or newer (found %s)" % sys.version.split()[0])

from orchestrator.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
