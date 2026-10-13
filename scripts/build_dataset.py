"""Build immutable training/validation/test scenarios or verify their frozen manifest."""

import argparse
import json
import sys
from pathlib import Path

from tool_agent_lab.evaluation.datasets import DEFAULT_SEED, build_dataset, validate_frozen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/scenarios/v1"))
    parser.add_argument("--profile", choices=("small", "full"), default="full")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--verify", action="store_true", help="Read and verify an existing dataset; never modify it")
    args = parser.parse_args(argv)
    manifest = validate_frozen(args.output) if args.verify else build_dataset(args.output, seed=args.seed, profile=args.profile)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"version": manifest["version"], "frozen": manifest["frozen"], "profile": manifest["profile"],
                      "seed": manifest["seed"], "splits": manifest["splits"], "index_fingerprints": manifest["index_fingerprints"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
