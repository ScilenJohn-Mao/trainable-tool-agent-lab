"""Run configuration checks locally or dispatch to the explicitly prepared ART environment."""

from pathlib import Path
import sys


def main(argv=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training/art"))
    from art_trainlab.train import main as training_main

    return training_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
