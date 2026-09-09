import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from chunking import git_state


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


def _repo(tmp: Path) -> Path:
    repo = tmp / "demo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "a.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return repo


class GitStateTests(unittest.TestCase):
    def test_active_branch_reads_the_checked_out_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            self.assertEqual(git_state.active_branch(str(repo)), "main")

    def test_active_branch_is_none_outside_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_state.active_branch(tmp))

    def test_resolve_branch_accepts_the_declared_branch_when_it_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            self.assertEqual(git_state.resolve_branch(str(repo), "main", False), "main")

    def test_resolve_branch_refuses_a_branch_that_is_not_checked_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            with self.assertRaises(SystemExit):
                git_state.resolve_branch(str(repo), "dev", False)

    def test_current_tree_takes_the_active_branch_and_ignores_the_declared_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            self.assertEqual(git_state.resolve_branch(str(repo), None, True), "main")
            self.assertEqual(git_state.resolve_branch(str(repo), "dev", True), "main")

    def test_resolve_branch_requires_one_of_the_two_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            with self.assertRaises(SystemExit):
                git_state.resolve_branch(str(repo), None, False)


class GetChunksLeavesGitAloneTests(unittest.TestCase):
    def test_chunking_get_does_not_move_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            (repo / "b.py").write_text("def f():\n    return 1\n")  # árbol sucio a propósito
            before = (
                _git(repo, "rev-parse", "HEAD"),
                _git(repo, "branch", "--show-current"),
                _git(repo, "status", "--porcelain"),
                _git(repo, "reflog", "--format=%H"),
            )
            out = subprocess.run(
                [sys.executable, "-m", "chunking.get_chunks", "--current-tree",
                 "--repo", str(repo), "--output", str(root / "chunks")],
                capture_output=True, text=True,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            after = (
                _git(repo, "rev-parse", "HEAD"),
                _git(repo, "branch", "--show-current"),
                _git(repo, "status", "--porcelain"),
                _git(repo, "reflog", "--format=%H"),
            )
            self.assertEqual(before, after)
            self.assertTrue((root / "chunks" / "main").is_dir())

    def test_chunking_get_refuses_a_branch_that_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            out = subprocess.run(
                [sys.executable, "-m", "chunking.get_chunks", "dev",
                 "--repo", str(repo), "--output", str(root / "chunks")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("checkout dev", out.stdout + out.stderr)
            self.assertEqual(_git(repo, "branch", "--show-current"), "main")
