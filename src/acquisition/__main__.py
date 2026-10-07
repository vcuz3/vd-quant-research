"""Command-line entry point; supports both module and script execution."""
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.acquisition.acquire import main

if __name__ == "__main__":
    raise SystemExit(main())
