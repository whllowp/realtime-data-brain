"""`python -m brian` — same entry point as the `brian` console script."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
