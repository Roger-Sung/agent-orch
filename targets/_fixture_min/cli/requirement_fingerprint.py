"""requirement-fingerprint for the fixture target.

A production target may delegate the value to another runtime; this one is
pure Python because the fixture must run where that runtime does not.  The rejection domain is the
point of the wrapper, not the hash: a symlink, a non-UTF-8 name or a NUL byte
makes two consumers of the same tree disagree, so the answer is a refusal
rather than a digest computed over whichever view happened to be taken.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import canonical, emit, fail, sha256_file  # noqa: E402

TOP_LEVEL = ("source-request.md", "proposal.md", "design.md")


def collect(change_dir: Path) -> tuple[list[tuple[str, Path]], str | None]:
    files: list[tuple[str, Path]] = []
    for name in TOP_LEVEL:
        path = change_dir / name
        if path.is_symlink():
            return [], f"symlink in change root: {name}"
        if path.is_file():
            files.append((name, path))

    specs = change_dir / "specs"
    if specs.exists():
        if specs.is_symlink():
            return [], "specs root is a symlink"
        for root, dirnames, filenames in os.walk(specs):
            root_path = Path(root)
            for dirname in dirnames:
                if (root_path / dirname).is_symlink():
                    return [], f"symlink directory: {dirname}"
            for filename in sorted(filenames):
                path = root_path / filename
                if path.is_symlink():
                    return [], f"symlink file: {path.relative_to(change_dir)}"
                if not path.is_file():
                    return [], f"unsupported entry: {path.relative_to(change_dir)}"
                files.append((str(path.relative_to(change_dir)), path))
    return files, None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change-dir", required=True)
    args = parser.parse_args(argv)
    change_dir = Path(args.change_dir)

    files, problem = collect(change_dir)
    if problem:
        return fail(f"rejected: {problem}")

    payload = []
    blob = bytearray()
    for relpath, path in sorted(files, key=lambda item: item[0]):
        data = path.read_bytes()
        if b"\x00" in data:
            return fail(f"rejected: NUL byte in {relpath}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return fail(f"rejected: non-UTF-8 content in {relpath}")
        blob += relpath.encode() + b"\x00" + data + b"\x00"
        payload.append({"path": relpath, "sha256": sha256_file(path)})

    import hashlib

    digest = "sha256:" + hashlib.sha256(bytes(blob)).hexdigest()
    return emit({"algorithm": "fixture-min-requirement-v1", "digest": digest, "files": payload})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
