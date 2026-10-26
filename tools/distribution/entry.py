"""PyInstaller entry: keep import roots independent from the build machine."""
from agenthub_rt.launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
