from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_rules
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="hysight")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate rules without Hindsight or inference")
    validate.add_argument("rules_file", type=Path)
    validate.add_argument("--max-hops", type=int, default=1)
    status = commands.add_parser("status", help="Read a state database without modifying it")
    status.add_argument("state_path", type=Path)
    args = parser.parse_args()
    if args.command == "validate":
        rules = load_rules(args.rules_file, args.max_hops)
        result = {"valid": True, "rules": [r.as_dict() for r in rules]}
    else:
        result = Store(args.state_path).snapshot()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

