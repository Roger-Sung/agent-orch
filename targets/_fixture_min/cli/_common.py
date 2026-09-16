"""Shared helpers for the fixture target's CLIs. stdlib only, by contract."""
from __future__ import annotations

import hashlib
import json
import sys


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def emit(payload) -> int:
    """stdout carries JSON only; anything human goes to stderr (PLAN §4)."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


def fail(message: str, code: int = 2) -> int:
    sys.stderr.write(message + "\n")
    return code


def sha256_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
