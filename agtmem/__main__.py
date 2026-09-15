"""Allow `python -m agtmem` as well as the installed `agtmem` entry point."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
