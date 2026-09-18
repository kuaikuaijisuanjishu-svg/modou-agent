"""CLI entry point: python -m modou.mcp --allow-repo ..."""
from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
