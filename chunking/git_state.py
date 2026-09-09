"""Git state the indexer reads but never changes.

Both entry points label their output with a branch name. This module is the single place that
decides which label is legitimate, so neither command has to guess and neither can move the repo.
"""
from __future__ import annotations

import subprocess
import sys


BRANCH_HELP = ("Rama Git a procesar. Debe ser la rama activa: el indexador no cambia de rama. "
               "Omítela junto con --current-tree.")

CURRENT_TREE_HELP = ("Indexa la rama activa tal como está en disco, sin comparar con un nombre "
                     "declarado.")


#: Provenance of the tree a `chunking-get` run actually read, written beside its JSONL.
TREE_STATE_FILE = "tree_state.json"


def _git(repo_dir: str, *args: str) -> str:
    """`git <args>` in `repo_dir`, or "" when git cannot answer."""
    try:
        result = subprocess.run(["git", *args], cwd=repo_dir,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def active_branch(repo_dir: str) -> str | None:
    """The checked-out branch, or `None` outside a repo or on a detached HEAD."""
    return _git(repo_dir, "branch", "--show-current") or None


def tree_state(repo_dir: str) -> dict[str, object]:
    """Sha and dirtiness of the working tree as it is right now.

    `chunking-get` records this next to the chunks it produced, because the manifest is written
    later by `chunking-ingest`: sampling git again there would describe a tree that may have moved
    since, and label old content with a new sha.
    """
    return {"git_sha": _git(repo_dir, "rev-parse", "HEAD") or None,
            "dirty": bool(_git(repo_dir, "status", "--porcelain"))}


def resolve_branch(repo_dir: str, declared: str | None, current_tree: bool) -> str:
    """The branch label to write, or exit with what the operator has to run.

    The indexer never checks out and never pulls: a declared branch that is not the active one is
    a mistake to report, not a repo to move.
    """
    if not current_tree and not declared:
        sys.exit("❌ Falta el nombre de la rama. Pásala como argumento, o usa --current-tree "
                 "para indexar la rama activa tal como está en disco.")
    active = active_branch(repo_dir)
    if not active:
        sys.exit(f"❌ {repo_dir} no está en una rama (HEAD suelto o no es un repo Git).")
    if current_tree:
        return active
    if declared != active:
        sys.exit(f"❌ Pediste indexar '{declared}' pero el repo está en '{active}'. El indexador no "
                 f"cambia de rama ni hace pull. Ejecuta:\n"
                 f"    git -C {repo_dir} checkout {declared}\n"
                 f"    git -C {repo_dir} pull origin {declared}\n"
                 f"o vuelve a llamar con --current-tree para indexar '{active}'.")
    return declared
