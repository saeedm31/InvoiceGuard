"""Command line: python -m invoiceguard {build|run DOC-001|eval [--live]}"""

from __future__ import annotations

import json
import sys

from . import evals, simulate
from .config import Settings


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "build":
        labels = simulate.build(Settings().data_dir)
        print(f"Built {len(labels)} inbox invoices in {Settings().data_dir}")
        return 0
    if cmd == "run":
        from .service import open_service

        svc = open_service()
        r = svc.process(sys.argv[2])
        print(json.dumps({k: r[k] for k in ("case_id", "decision", "status", "exceptions", "note")}, indent=2))
        svc.hub.close()
        return 0
    if cmd == "eval":
        return evals.main(sys.argv[2:])
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
