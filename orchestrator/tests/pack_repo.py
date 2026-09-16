"""Synthetic git repositories for the pack-v1 identity fixtures.

The identity spec is written in terms of git plumbing, so the fixtures need a
real repository rather than a mocked one; every helper here builds a throwaway
repo with pinned identity and config so results do not depend on the developer's
~/.gitconfig.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def run_git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def write(repo: Path, relpath: str, content: bytes | str, *, executable: bool = False) -> Path:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    target.write_bytes(content)
    if executable:
        target.chmod(0o755)
    return target


def init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "fixture")
    run_git(root, "config", "user.email", "fixture@example.invalid")
    return root


def commit_all(repo: Path, message: str = "fixture") -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message, "--allow-empty")
    return run_git(repo, "rev-parse", "HEAD").strip()


def base_repo(root: Path) -> Path:
    """A repo with one tracked file in HEAD - the starting point for most cases."""
    init_repo(root)
    write(repo := root, "src/A.java", "class A {}\n")
    commit_all(repo)
    return repo
