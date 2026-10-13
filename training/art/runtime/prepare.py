"""Apply and verify the fixed ART runtime's dummy-run compatibility patch."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile


ART_REVISION = "12162f241320f2343f1df162e834dc5ae03f740d"
SOURCE_SHA256 = "05837957f1f0b9e21d487f844bb5e2122c26b2050692711f092b944c3552a9e4"
PATCH_NAME = "art-policy-spans-dummy-run-v1"
ANCHOR = """                    output = original(self, *args, **kwargs)
                    # The input batch is current only after execute_model, and the
"""
REPLACEMENT = """                    output = original(self, *args, **kwargs)
                    # Profiling/CUDA graph batches have no real policy provenance.
                    if kwargs.get("dummy_run", args[2] if len(args) > 2 else False):
                        if hasattr(self, _EXECUTING_POLICY_CONTEXT_FIELD):
                            delattr(self, _EXECUTING_POLICY_CONTEXT_FIELD)
                        return output
                    # The input batch is current only after execute_model, and the
"""


def rewrite(source: str) -> str:
    """Return the narrowly patched source; identity is checked before installation."""
    if source.count(ANCHOR) != 1:
        raise ValueError("Fixed runtime execute_model patch location differs")
    patched = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(patched, "policy_spans.py", "exec")
    return patched


def inspect_runtime() -> tuple[Path, dict]:
    if sys.platform != "linux":
        raise ValueError("Prepare the separate Linux ART runtime environment")
    distribution = importlib.metadata.distribution("art-vllm-runtime")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    versions = {"art-vllm-runtime": distribution.version,
                "vllm": importlib.metadata.version("vllm")}
    revision = direct.get("vcs_info", {}).get("commit_id")
    if versions != {"art-vllm-runtime": "0.1.0", "vllm": "0.25.1+cu129"} or revision != ART_REVISION:
        raise ValueError("Installed runtime version/revision differs from the fixed lock")
    path = Path(distribution.locate_file("art_vllm_runtime/policy_spans.py")).resolve()
    if not path.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("Runtime source must belong to this Python environment")
    return path, {"versions": versions, "revision": revision}


def prepare(*, apply: bool = False) -> dict:
    path, report = inspect_runtime()
    original = path.read_bytes()
    source = original.decode("utf-8")
    digest = hashlib.sha256(original).hexdigest()
    if digest == SOURCE_SHA256:
        patched = rewrite(source).encode("utf-8")
        applied = False
    elif source.count(REPLACEMENT) == 1:
        restored = source.replace(REPLACEMENT, ANCHOR, 1).encode("utf-8")
        if hashlib.sha256(restored).hexdigest() != SOURCE_SHA256:
            raise ValueError("Runtime source differs beyond the declared compatibility patch")
        patched = original
        applied = True
    else:
        raise ValueError("Runtime source SHA-256 differs from the pinned ART source")
    patched_digest = hashlib.sha256(patched).hexdigest()
    changed = apply and not applied
    if changed:
        # Replace only a verified source file, never an import cache's shared inode.
        descriptor, temporary = tempfile.mkstemp(prefix=".policy-spans-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(patched)
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        applied = True
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != patched_digest:
            raise RuntimeError("Runtime compatibility patch verification failed")
    report["compatibility_patch"] = {"name": PATCH_NAME, "applied": applied,
        "changed": changed, "source_path": str(path), "original_sha256": SOURCE_SHA256,
        "source_sha256": digest, "patched_sha256": patched_digest}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="Patch the pinned installed runtime before starting ART")
    action.add_argument("--check", action="store_true", help="Verify the installed patch without importing GPU libraries")
    args = parser.parse_args(argv)
    report = prepare(apply=args.apply)
    print(json.dumps(report, indent=2))
    if not report["compatibility_patch"]["applied"]:
        print("Apply the runtime compatibility patch with this script's --apply option before training", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
