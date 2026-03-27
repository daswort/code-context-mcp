"""
Carga de configuración por proyecto.

Busca un archivo `.chunking.yaml` en la raíz del repo objetivo y lo mergea
con los valores por defecto. Los valores del YAML *extienden* los defaults,
nunca los reemplazan.
"""

import os
import yaml

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", "dist", "build",
    ".venv", ".idea", ".vscode", ".github", "bin", "obj", "chunks",
}

DEFAULT_EXCLUDE_EXT = {
    ".exe", ".bin", ".dll", ".pdb", ".user",
    ".jpg", ".jpeg", ".png", ".gif",
    ".zip", ".tar", ".gz", ".lock",
}

DEFAULT_EXCLUDE_FILES = {
    "requirements.txt", "package-lock.json",
}

DEFAULT_VALID_EXT = {
    ".cs", ".csproj", ".sln", ".cshtml",
    ".js", ".ts",
    ".md", ".json", ".yaml", ".yml", ".txt", ".http",
}

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100

DEFAULT_CHROMA_HOST = "localhost"
DEFAULT_CHROMA_PORT = 8000
DEFAULT_CHROMA_AUTH_TOKEN = ""
DEFAULT_COLLECTION_PREFIX = "repo"

DEFAULT_HNSW_SPACE = "cosine"
DEFAULT_HNSW_EF_CONSTRUCTION = 200
DEFAULT_HNSW_EF_SEARCH = 150


# ─── Loader ──────────────────────────────────────────────────────────────────

def load_config(repo_dir: str) -> dict:
    """
    Lee `.chunking.yaml` desde *repo_dir* (si existe) y devuelve un dict
    con la configuración resultante (defaults + overrides).
    """
    cfg_path = os.path.join(repo_dir, ".chunking.yaml")

    user_cfg: dict = {}
    if os.path.isfile(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}

    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
    exclude_dirs.update(user_cfg.get("exclude_dirs", []))

    exclude_ext = set(DEFAULT_EXCLUDE_EXT)
    exclude_ext.update(user_cfg.get("exclude_ext", []))

    exclude_files = set(DEFAULT_EXCLUDE_FILES)
    exclude_files.update(user_cfg.get("exclude_files", []))

    valid_ext = set(DEFAULT_VALID_EXT)
    valid_ext.update(user_cfg.get("extra_valid_ext", []))

    return {
        "exclude_dirs": exclude_dirs,
        "exclude_ext": exclude_ext,
        "exclude_files": exclude_files,
        "valid_ext": valid_ext,
        "chunk_size": user_cfg.get("chunk_size", DEFAULT_CHUNK_SIZE),
        "chunk_overlap": user_cfg.get("chunk_overlap", DEFAULT_CHUNK_OVERLAP),
        "chroma_host": user_cfg.get("chroma_host", DEFAULT_CHROMA_HOST),
        "chroma_port": int(user_cfg.get("chroma_port", DEFAULT_CHROMA_PORT)),
        "chroma_auth_token": user_cfg.get("chroma_auth_token", DEFAULT_CHROMA_AUTH_TOKEN),
        "collection_prefix": user_cfg.get("collection_prefix", DEFAULT_COLLECTION_PREFIX),
        "hnsw_space": user_cfg.get("hnsw_space", DEFAULT_HNSW_SPACE),
        "hnsw_ef_construction": int(user_cfg.get("hnsw_ef_construction", DEFAULT_HNSW_EF_CONSTRUCTION)),
        "hnsw_ef_search": int(user_cfg.get("hnsw_ef_search", DEFAULT_HNSW_EF_SEARCH)),
    }

