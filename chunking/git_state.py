"""Git state the indexer reads but never changes.

Both entry points label their output with a branch name. This module is the single place that
decides which label is legitimate, so neither command has to guess and neither can move the repo.
"""
from __future__ import annotations

import subprocess
import sys


def active_branch(repo_dir: str) -> str | None:
    """The checked-out branch, or `None` outside a repo or on a detached HEAD."""
    try:
        result = subprocess.run(["git", "branch", "--show-current"], cwd=repo_dir,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def resolve_branch(repo_dir: str, declared: str | None, current_tree: bool) -> str:
    """The branch label to write, or exit with what the operator has to run.

    The indexer never checks out and never pulls: a declared branch that is not the active one is
    a mistake to report, not a repo to move.
    """
    active = active_branch(repo_dir)
    if current_tree:
        if not active:
            sys.exit(f"❌ {repo_dir} no está en una rama (HEAD suelto o no es un repo Git).")
        return active
    if not declared:
        sys.exit("❌ Falta el nombre de la rama. Pásala como argumento, o usa --current-tree "
                 "para indexar la rama activa tal como está en disco.")
    if not active:
        sys.exit(f"❌ {repo_dir} no está en una rama (HEAD suelto o no es un repo Git).")
    if declared != active:
        sys.exit(f"❌ Pediste indexar '{declared}' pero el repo está en '{active}'. El indexador no "
                 f"cambia de rama ni hace pull. Ejecuta:\n"
                 f"    git -C {repo_dir} checkout {declared}\n"
                 f"    git -C {repo_dir} pull origin {declared}\n"
                 f"o vuelve a llamar con --current-tree para indexar '{active}'.")
    return declared
