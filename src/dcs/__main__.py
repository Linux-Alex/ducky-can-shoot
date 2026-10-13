"""Allow 'python -m dcs' to behave exactly like the dcs script."""

from __future__ import annotations

import sys

from dcs.cli import main

if __name__ == "__main__":
    sys.exit(main())
