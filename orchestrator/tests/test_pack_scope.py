"""IDENTITIES §2.3 scope expansion, protections and ignore binding (step 0a)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.errors import IdentityRefused
from orchestrator.pack.identities import (
    Rejections,
    check_ignore_binding,
    classify_scope_item,
    compose_excludes,
    glob_match,
    managed_set,
    scan_scope,
    tree_manifest,
)
from orchestrator.tests.pack_repo import base_repo, commit_all, init_repo, run_git, write


class ScopeClassificationTest(unittest.TestCase):
    def test_kind_is_decided_by_the_literal(self) -> None:
        self.assertEqual(classify_scope_item("src/A.java"), "file")
        self.assertEqual(classify_scope_item("src/"), "dir")
        self.assertEqual(classify_scope_item("src/**/*.java"), "glob")
        self.assertEqual(classify_scope_item("src/?.java"), "glob")
        # readable_roots entries are directories whatever they look like.
        self.assertEqual(classify_scope_item("src", always_dir=True), "dir")

    # ID14g: `*` never crosses a segment; `**` may match zero segments.
    def test_id14g_glob_segment_rules(self) -> None:
        self.assertFalse(glob_match("src/*.java", "src/x/y.java"))
        self.assertTrue(glob_match("src/**/*.java", "src/A.java"))
        self.assertTrue(glob_match("src/**/*.java", "src/a/b/C.java"))


class ScopeProtectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = base_repo(self.root / "repo")

    def managed(self, excludes: bytes = b"") -> set[bytes]:
        m_head, others, _ = managed_set(self.repo, excludes)
        return m_head | others

    def scan(self, **kwargs) -> set[str]:
        excludes = kwargs.pop("excludes", b"")
        return scan_scope(self.repo, self.managed(excludes), **kwargs)

    def expect_refusal(self, code: str, **kwargs) -> None:
        with self.assertRaises(IdentityRefused) as ctx:
            self.scan(**kwargs)
        self.assertEqual(ctx.exception.code, code)

    # ID14b / ID14d: an excluded but protected source file inside scope is fatal -
    # it would otherwise be editable yet invisible to the fingerprint.
    def test_id14d_literal_file_excluded_but_protected(self) -> None:
        write(self.repo, ".gitignore", "B.java\n")
        commit_all(self.repo, "ignore B")
        write(self.repo, "src/B.java", "class B {}\n")
        self.expect_refusal(
            "excluded_source_in_scope",
            files_writable=["src/B.java"],
            protected_source_globs=["**/*.java"],
            excludes=compose_excludes([], ["src/B.java"]),
        )

    # ID14d-2: the same holds when the scope item is a readable root.
    def test_id14d2_readable_root_excluded_but_protected(self) -> None:
        write(self.repo, "src/B.java", "class B {}\n")
        self.expect_refusal(
            "excluded_source_in_scope",
            readable_roots=["src"],
            protected_source_globs=["**/*.java"],
            excludes=compose_excludes([], ["src/B.java"]),
        )

    # ID14d-3: a nested repo that no scope item can reach is not our business.
    def test_id14d3_nested_repo_outside_reachable_set(self) -> None:
        nested = init_repo(self.repo / "src" / "vendor")
        write(nested, "x.txt", "x\n")
        commit_all(nested)
        selected = self.scan(
            files_writable=["src/A.java"],
            protected_source_globs=["**/*.java"],
            excludes=compose_excludes([], ["src/vendor/"]),
        )
        self.assertEqual(selected, {"src/A.java"})

    # ID14e-1: a symlinked directory inside a readable root.
    def test_id14e1_symlink_dir_in_readable_root(self) -> None:
        (self.repo / "other").mkdir()
        os.symlink("../other", self.repo / "src" / "link")
        self.expect_refusal("symlink_dir_in_scope", readable_roots=["src"])

    # ID14e-2: the same under a glob's literal prefix.
    def test_id14e2_symlink_dir_under_glob_prefix(self) -> None:
        (self.repo / "other").mkdir()
        os.symlink("../other", self.repo / "src" / "link")
        self.expect_refusal("symlink_dir_in_scope", files_writable=["src/**/*.java"])

    # ID14e-3: an ancestor of the scope item is itself a symlink.
    def test_id14e3_symlink_ancestor(self) -> None:
        (self.repo / "real").mkdir()
        (self.repo / "real" / "main").mkdir()
        os.symlink("real", self.repo / "aliased")
        self.expect_refusal("symlink_dir_in_scope", readable_roots=["aliased/main"])

    # A nested repo the scope *can* reach is refused.
    def test_nested_repo_in_scope(self) -> None:
        nested = init_repo(self.repo / "src" / "vendor")
        write(nested, "x.txt", "x\n")
        commit_all(nested)
        with self.assertRaises(IdentityRefused) as ctx:
            self.scan(
                readable_roots=["src"],
                excludes=compose_excludes([], ["src/vendor/"]),
            )
        self.assertEqual(ctx.exception.code, "nested_repo_in_scope")

    def test_scope_selects_expected_files(self) -> None:
        write(self.repo, "src/sub/B.java", "class B {}\n")
        write(self.repo, "src/notes.txt", "n\n")
        commit_all(self.repo, "more files")
        self.assertEqual(
            self.scan(files_writable=["src/**/*.java"]),
            {"src/A.java", "src/sub/B.java"},
        )


class IgnoreBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = base_repo(self.root / "repo")

    def check(self, excludes: bytes = b"") -> Rejections:
        m_head, others, _ = managed_set(self.repo, excludes)
        rejections = Rejections()
        check_ignore_binding(self.repo, m_head | others, excludes, rejections)
        return rejections

    # ID14f: an unbound .gitignore could change what is hashed without changing
    # the fingerprint.
    def test_id14f_unbound_gitignore_is_refused(self) -> None:
        write(self.repo, ".gitignore", "*.gitignore\n")
        commit_all(self.repo, "parent ignores nested ignore files")
        write(self.repo, "src/.gitignore", "*.tmp\n")
        with self.assertRaises(IdentityRefused) as ctx:
            self.check().raise_first()
        self.assertEqual(ctx.exception.code, "ignore_file_not_bound")

    # ID14f-2: inside an excluded directory it takes no part in matching.
    def test_id14f2_gitignore_inside_excluded_directory(self) -> None:
        write(self.repo, "build/.gitignore", "*.tmp\n")
        excludes = compose_excludes(["build/"], [])
        self.assertFalse(bool(self.check(excludes)))

    # ID14f-3: a tracked .gitignore is bound even when a parent rule matches it.
    def test_id14f3_tracked_gitignore_is_bound(self) -> None:
        write(self.repo, ".gitignore", "*.gitignore\n")
        write(self.repo, "src/.gitignore", "*.tmp\n")
        # The parent rule matches both ignore files (including itself), so they
        # only reach HEAD via -f; that is exactly the case this fixture is for.
        run_git(self.repo, "add", "-f", ".gitignore", "src/.gitignore")
        commit_all(self.repo, "track both")
        self.assertFalse(bool(self.check()))

    def test_tree_manifest_reports_unbound_ignore(self) -> None:
        write(self.repo, ".gitignore", "*.gitignore\n")
        commit_all(self.repo, "parent rule")
        write(self.repo, "src/.gitignore", "*.tmp\n")
        with self.assertRaises(IdentityRefused) as ctx:
            tree_manifest(self.repo)
        self.assertEqual(ctx.exception.code, "ignore_file_not_bound")


if __name__ == "__main__":
    unittest.main()
