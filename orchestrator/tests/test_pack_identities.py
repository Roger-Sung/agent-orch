"""IDENTITIES §8 fixtures for candidate_fingerprint (IMPLEMENTATION-PLAN step 0a)."""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.errors import ContractRejected, IdentityRefused
from orchestrator.pack.identities import (
    CANDIDATE_MAGIC,
    candidate_fingerprint,
    compose_excludes,
    existence_set,
    tree_manifest,
)
from orchestrator.tests.pack_repo import base_repo, commit_all, init_repo, run_git, write


class CandidateFingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = base_repo(self.root / "repo")

    def fp(self, excludes: bytes = b"") -> str:
        return candidate_fingerprint(self.repo, excludes)

    # ID8: the same tree hashes the same twice.
    def test_id8_same_tree_twice(self) -> None:
        self.assertEqual(self.fp(), self.fp())

    # ID8b: config that git would otherwise honour must not move the result.
    def test_id8b_ignores_global_config_and_info_exclude(self) -> None:
        before = self.fp()
        (self.repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("*.java\n")
        run_git(self.repo, "config", "core.ignoreCase", "true")
        self.assertEqual(self.fp(), before)

    # ID9: staging an existing modification changes nothing.
    def test_id9_staged_vs_unstaged_modification(self) -> None:
        write(self.repo, "src/A.java", "class A { int x; }\n")
        unstaged = self.fp()
        run_git(self.repo, "add", "src/A.java")
        self.assertEqual(self.fp(), unstaged)

    # ID9b: a new untracked file counts whether or not it is added.
    def test_id9b_untracked_vs_added(self) -> None:
        write(self.repo, "src/B.java", "class B {}\n")
        untracked = self.fp()
        run_git(self.repo, "add", "src/B.java")
        self.assertEqual(self.fp(), untracked)

    # ID9c: a deleted tracked file is `A` with or without `git rm --cached`.
    def test_id9c_deleted_tracked_file(self) -> None:
        (self.repo / "src/A.java").unlink()
        plain = self.fp()
        run_git(self.repo, "rm", "--cached", "-q", "src/A.java")
        self.assertEqual(self.fp(), plain)
        manifest = tree_manifest(self.repo)
        self.assertEqual(manifest["entries"][b"src/A.java"][:1], b"A")
        self.assertNotIn(b"src/A.java", existence_set(manifest))

    # ID9d: force-adding an ignored file does not pull it into M.
    def test_id9d_force_added_ignored_file(self) -> None:
        write(self.repo, ".gitignore", "ignored.txt\n")
        commit_all(self.repo, "add ignore rule")
        write(self.repo, "ignored.txt", "x\n")
        before = self.fp()
        run_git(self.repo, "add", "-f", "ignored.txt")
        self.assertEqual(self.fp(), before)

    # ID9e: an index-only file removed from disk equals never having added it.
    def test_id9e_index_only_then_removed(self) -> None:
        before = self.fp()
        write(self.repo, "src/C.java", "class C {}\n")
        run_git(self.repo, "add", "src/C.java")
        (self.repo / "src/C.java").unlink()
        self.assertEqual(self.fp(), before)

    # ID10 / ID11: the exec bit is part of the kind.
    def test_id10_exec_bit(self) -> None:
        write(self.repo, "tool.sh", "#!/bin/sh\n")
        commit_all(self.repo, "add tool")
        before = self.fp()
        os.chmod(self.repo / "tool.sh", 0o755)
        self.assertNotEqual(self.fp(), before)
        self.assertEqual(tree_manifest(self.repo)["entries"][b"tool.sh"][:1], b"X")

    # ID12: a rename changes the path bytes, so the body changes.
    def test_id12_rename(self) -> None:
        before = self.fp()
        (self.repo / "src/A.java").rename(self.repo / "src/Renamed.java")
        self.assertNotEqual(self.fp(), before)

    # ID13 / ID13b: symlinks hash their link text, and swapping kind changes it.
    def test_id13_symlink_target_and_kind(self) -> None:
        os.symlink("A.java", self.repo / "src/link")
        commit_all(self.repo, "add link")
        manifest = tree_manifest(self.repo)
        self.assertEqual(manifest["entries"][b"src/link"][:1], b"L")
        before = manifest["candidate_fingerprint"]

        (self.repo / "src/link").unlink()
        os.symlink("Other.java", self.repo / "src/link")
        retargeted = self.fp()
        self.assertNotEqual(retargeted, before)

        (self.repo / "src/link").unlink()
        write(self.repo, "src/link", "A.java")
        self.assertNotEqual(self.fp(), retargeted)
        self.assertEqual(tree_manifest(self.repo)["entries"][b"src/link"][:1], b"F")

    # ID14: an excluded, unprotected untracked file is outside M.
    def test_id14_excluded_untracked_file(self) -> None:
        before = self.fp()
        write(self.repo, "build/out.txt", "a\n")
        excludes = compose_excludes(["build/"], [])
        after = candidate_fingerprint(self.repo, excludes)
        self.assertEqual(after, before)
        write(self.repo, "build/out.txt", "b\n")
        self.assertEqual(candidate_fingerprint(self.repo, excludes), before)

    # ID14c: .git/info/exclude must not remove a path from M.
    def test_id14c_info_exclude_keeps_path(self) -> None:
        write(self.repo, "src/B.java", "class B {}\n")
        with_file = self.fp()
        (self.repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("src/B.java\n")
        self.assertEqual(self.fp(), with_file)
        self.assertIn(b"src/B.java", tree_manifest(self.repo)["entries"])

    # ID16: large content with NUL bytes is ordinary input.
    def test_id16_large_binary_content(self) -> None:
        write(self.repo, "blob.bin", (b"\0\x01\x02" * 400_000))
        manifest = tree_manifest(self.repo)
        self.assertEqual(manifest["entries"][b"blob.bin"][:1], b"F")

    # ID16b: length-prefixed records cannot be forged by embedding entry bytes.
    def test_id16b_length_prefix_prevents_forgery(self) -> None:
        one = init_repo(self.root / "one")
        forged = b"x" + struct.pack(">I", len(b"b")) + b"b" + b"F" + (b"\0" * 32)
        write(one, "a", forged)
        commit_all(one)

        two = init_repo(self.root / "two")
        write(two, "a", b"x")
        write(two, "b", b"y")
        commit_all(two)

        self.assertNotEqual(
            tree_manifest(one)["tree_manifest_sha256"],
            tree_manifest(two)["tree_manifest_sha256"],
        )

    # ID19: the encoding itself, independent of any repo layout.
    def test_id19_golden_encoding(self) -> None:
        manifest = tree_manifest(self.repo)
        entry = manifest["entries"][b"src/A.java"]
        expected_body = struct.pack(">I", len(b"src/A.java")) + b"src/A.java" + entry
        self.assertEqual(manifest["body"], expected_body)

        head_bytes = bytes.fromhex(manifest["head"])
        expected_header = CANDIDATE_MAGIC + struct.pack(">B", len(head_bytes)) + head_bytes
        import hashlib

        self.assertEqual(
            manifest["candidate_fingerprint"],
            "sha256:" + hashlib.sha256(expected_header + expected_body).hexdigest(),
        )
        self.assertEqual(
            manifest["tree_manifest_sha256"], hashlib.sha256(expected_body).hexdigest()
        )

    # ID18: a new commit changes the header, so the fingerprint moves with base.
    def test_id18_head_moves(self) -> None:
        before = self.fp()
        write(self.repo, "src/A.java", "class A { }\n")
        commit_all(self.repo, "second")
        self.assertNotEqual(self.fp(), before)

    # ID22b: two managed paths sharing an inode are refused before anything else.
    def test_id22b_hardlink_aliasing(self) -> None:
        write(self.repo, "src/B.java", "class B {}\n")
        os.link(self.repo / "src/B.java", self.repo / "src/C.java")
        commit_all(self.repo, "hardlink")
        with self.assertRaises(IdentityRefused) as ctx:
            tree_manifest(self.repo)
        self.assertEqual(ctx.exception.code, "path_aliasing")

    # A tracked file replaced by a FIFO cannot be represented (joint-r1 ID41a).
    # An *untracked* FIFO is a different case: `ls-files --others` never lists
    # one, so it is simply outside M and changes nothing.
    def test_unsupported_entry_is_refused(self) -> None:
        write(self.repo, "pipe", "placeholder\n")
        commit_all(self.repo, "track pipe path")
        (self.repo / "pipe").unlink()
        os.mkfifo(self.repo / "pipe")
        with self.assertRaises(IdentityRefused) as ctx:
            tree_manifest(self.repo)
        self.assertEqual(ctx.exception.code, "unsupported_entry")

    def test_untracked_fifo_is_outside_m(self) -> None:
        before = self.fp()
        os.mkfifo(self.repo / "pipe")
        self.assertEqual(self.fp(), before)

    # An untracked nested repo collapses to a directory entry and is refused.
    def test_nested_repo_untracked_is_refused(self) -> None:
        nested = init_repo(self.repo / "vendor" / "lib")
        write(nested, "x.txt", "x\n")
        commit_all(nested)
        with self.assertRaises(IdentityRefused) as ctx:
            tree_manifest(self.repo)
        self.assertEqual(ctx.exception.code, "nested_repo_untracked")


class ExcludeCompositionTest(unittest.TestCase):
    # ID14h: negation would let a .gitignore re-include an engine exclusion.
    def test_id14h_negation_rejected(self) -> None:
        with self.assertRaises(ContractRejected) as ctx:
            compose_excludes([], ["!pack-state.json"])
        self.assertEqual(ctx.exception.code, "invalid_exclude_pattern")

    def test_engine_patterns_come_first(self) -> None:
        body = compose_excludes(["pack-state.json"], ["build/"])
        self.assertEqual(body, b"pack-state.json\nbuild/\n")

    def test_empty_and_comment_patterns_rejected(self) -> None:
        for bad in ("", "   ", "#comment"):
            with self.assertRaises(ContractRejected):
                compose_excludes([], [bad])


if __name__ == "__main__":
    unittest.main()
