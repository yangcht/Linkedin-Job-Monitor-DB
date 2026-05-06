from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .cli import main as cli_main


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    ensure_local_config()
    if not args:
        args = ["serve", "--open"]
    return cli_main(args)


def ensure_local_config() -> None:
    config = Path("config.ini")
    example = Path("config.example.ini")
    if config.exists() or not example.exists():
        return
    shutil.copyfile(example, config)


if __name__ == "__main__":
    raise SystemExit(main())
