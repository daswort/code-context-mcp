import json
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


class IngestManifestCannotLieTests(unittest.TestCase):
    def test_manifest_records_the_tree_that_was_chunked_not_the_one_at_ingest_time(self) -> None:
        # chunking-get corre sobre un arbol y chunking-ingest escribe el manifiesto despues:
        # si el manifiesto muestreara git de nuevo, etiquetaria contenido viejo con un sha nuevo
        from chunking.config import load_config
        from chunking.git_state import TREE_STATE_FILE
        from chunking.ingest_delta import collection_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            chunks = root / "chunks"
            out = subprocess.run(
                [sys.executable, "-m", "chunking.get_chunks", "--current-tree",
                 "--repo", str(repo), "--output", str(chunks)],
                capture_output=True, text=True,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            chunked_sha = _git(repo, "rev-parse", "HEAD")
            recorded = json.loads((chunks / "main" / TREE_STATE_FILE).read_text())
            self.assertEqual(recorded["git_sha"], chunked_sha)

            (repo / "a.txt").write_text("moved on")
            _git(repo, "add", "a.txt")
            _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "v2")
            self.assertNotEqual(_git(repo, "rev-parse", "HEAD"), chunked_sha)

            manifest = collection_manifest(
                str(repo), "main", "demo_main", load_config(str(repo)), {"a.txt:0": "h"}, recorded)
            self.assertEqual(manifest["git_sha"], chunked_sha)
            self.assertFalse(manifest["dirty"])

    def test_manifest_branch_is_the_real_name_not_the_sanitized_one(self) -> None:
        # el directorio y la coleccion se sanitizan (feature-x); el campo branch guarda feature/x
        from chunking.config import load_config
        from chunking.git_state import active_branch
        from chunking.ingest_delta import collection_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            _git(repo, "checkout", "-q", "-b", "feature/x")
            manifest = collection_manifest(
                str(repo), "feature/x", "demo_feature-x", load_config(str(repo)), {"a.txt:0": "h"})
            self.assertEqual(manifest["branch"], "feature/x")
            self.assertEqual(manifest["branch"], active_branch(str(repo)))
            self.assertEqual(manifest["git_sha"], _git(repo, "rev-parse", "HEAD"))

    def test_manifest_refuses_to_contradict_its_own_provenance(self) -> None:
        # el cableado de main() pasa la rama real; si alguien pasara la sanitizada, truena aca
        from chunking.config import load_config
        from chunking.ingest_delta import collection_manifest
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            recorded = {"git_sha": _git(repo, "rev-parse", "HEAD"), "dirty": False,
                        "branch": "feature/x"}
            with self.assertRaises(ValueError):
                collection_manifest(str(repo), "feature-x", "demo_feature-x",
                                    load_config(str(repo)), {"a.txt:0": "h"}, recorded)

    def test_ingest_without_chunks_does_not_reach_chroma(self) -> None:
        # sin JSONL no hay nada que ingerir: sale antes de crear la coleccion vacia
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            out = subprocess.run(
                [sys.executable, "-m", "chunking.ingest_delta", "--current-tree",
                 "--repo", str(repo), "--chunks-dir", str(root / "chunks")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("chunking-get", out.stdout + out.stderr)
            self.assertNotIn("Conectando a ChromaDB", out.stdout + out.stderr)


    def test_ingest_refuses_a_branch_that_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _repo(root)
            out = subprocess.run(
                [sys.executable, "-m", "chunking.ingest_delta", "dev",
                 "--repo", str(repo), "--chunks-dir", str(root / "chunks")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("está en 'main'", out.stdout + out.stderr)
            # la guarda corre antes de crear el cliente: no debe haber intento de conexión
            self.assertNotIn("Conectando a ChromaDB", out.stdout + out.stderr)
