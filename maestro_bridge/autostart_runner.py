"""Absolute-path entry point for login startup, independent of the login cwd."""
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from maestro_bridge.cli import main
    raise SystemExit(main())
