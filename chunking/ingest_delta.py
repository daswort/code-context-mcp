#!/usr/bin/env python3
"""
Actualiza embeddings de una rama específica en ChromaDB (servidor HTTP).
ChromaDB genera los embeddings server-side — el cliente solo envía texto.

Uso:
    chunking-ingest <branch> [--repo .] [--chunks-dir ./chunks]
                              [--chroma-host localhost] [--chroma-port 8000]
                              [--collection-prefix repo]
"""

import os
import sys
import json
import hashlib
import argparse
import subprocess
from datetime import datetime, timezone
from importlib.metadata import version

from tqdm import tqdm
import chromadb
from chromadb.config import Settings

from chunking.config import load_config
from chunking.git_state import BRANCH_HELP, CURRENT_TREE_HELP, resolve_branch


# ─── Constants ───────────────────────────────────────────────────────────────

INGEST_BATCH_SIZE = 100
CHUNK_METADATA_VERSION = "2"

LANG_MAP = {
    ".py": "python", ".go": "go", ".ts": "typescript", ".js": "javascript",
    ".cs": "csharp", ".cshtml": "csharp", ".csproj": "xml", ".sln": "xml",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".html": "html", ".css": "css", ".sql": "tsql", ".txt": "text", ".http": "http",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_text(text: str) -> str:
    """Devuelve un hash MD5 del texto."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def chunk_state_hash(record: dict) -> str:
    """Incluye la versión de metadata para reindexar una vez tras cambios de schema."""
    return hash_text(f"{CHUNK_METADATA_VERSION}\0{record['content']}")


def build_metadata(rec: dict) -> dict:
    """Construye metadatos enriquecidos para un chunk."""
    file_path = rec["file"]
    ext = os.path.splitext(file_path)[1]
    metadata = {
        "file": file_path,
        "chunk_id": rec["chunk_id"],
        "tokens": rec["tokens"],
        "ext": ext,
        "language": LANG_MAP.get(ext, "other"),
        "directory": os.path.dirname(file_path),
        "filename": os.path.basename(file_path),
    }
    for field in ("start_line", "end_line", "symbol"):
        if field in rec:
            metadata[field] = rec[field]
    return metadata


def get_last_chunks_file(branch_dir: str) -> str:
    """Obtiene el último archivo *_chunks.jsonl en la carpeta de la rama."""
    if not os.path.exists(branch_dir):
        raise FileNotFoundError(f"No existe la carpeta {branch_dir}")

    files = [f for f in os.listdir(branch_dir) if f.endswith("_chunks.jsonl")]
    if not files:
        raise FileNotFoundError(f"No hay archivos *_chunks.jsonl en {branch_dir}")

    files.sort()
    return os.path.join(branch_dir, files[-1])


def _state_key_to_chroma_id(state_key: str) -> str:
    """Convierte key del state (file:chunk_id) a ID de ChromaDB (file-chunk_id)."""
    # La key es "file:chunk_id" — el último ":" separa el chunk_id
    last_colon = state_key.rfind(":")
    file_path = state_key[:last_colon]
    chunk_id = state_key[last_colon + 1:]
    return f"{file_path}-{chunk_id}"


def detect_changes(
    branch_dir: str, reingest_all: bool = False
) -> tuple[list[dict], list[str], dict[str, str]]:
    """Detecta fragmentos nuevos/modificados y eliminados.

    Returns:
        (changed, deleted_ids, new_state): chunks a upsert, IDs a eliminar y estado pendiente.
    """
    chunks_file = get_last_chunks_file(branch_dir)
    state_file = os.path.join(branch_dir, "last_state.json")

    print(f"🔍 Analizando cambios en {chunks_file}")

    with open(chunks_file, "r", encoding="utf-8") as f:
        curr = [json.loads(line) for line in f]

    if reingest_all:
        print("⚠️ La colección está vacía. Se reingestarán todos los fragmentos.")
        new_state = {f"{c['file']}:{c['chunk_id']}": chunk_state_hash(c) for c in curr}
        return curr, [], new_state

    if not os.path.exists(state_file):
        print("⚠️ No previous state found. All chunks will be re-ingested.")
        new_state = {f"{c['file']}:{c['chunk_id']}": chunk_state_hash(c) for c in curr}
        return curr, [], new_state

    with open(state_file, "r", encoding="utf-8") as f:
        prev = json.load(f)

    changed: list[dict] = []
    new_state: dict[str, str] = {}
    for c in curr:
        key = f"{c['file']}:{c['chunk_id']}"
        h = chunk_state_hash(c)
        new_state[key] = h
        if prev.get(key) != h:
            changed.append(c)

    # Detectar chunks eliminados (existían antes pero ya no)
    deleted_keys = set(prev.keys()) - set(new_state.keys())
    deleted_ids = [_state_key_to_chroma_id(k) for k in deleted_keys]

    if changed:
        print(f"♻️ {len(changed)} fragmentos modificados o nuevos detectados.")
    if deleted_ids:
        print(f"🗑️ {len(deleted_ids)} fragmentos eliminados detectados.")
    if not changed and not deleted_ids:
        print("✅ No se detectaron cambios en los fragmentos.")

    return changed, deleted_ids, new_state


def save_state(branch_dir: str, state: dict[str, str]) -> None:
    """Guarda el estado sólo después de que Chroma acepte la actualización."""
    save_json_atomically(os.path.join(branch_dir, "last_state.json"), state)


def save_json_atomically(path: str, data: dict) -> None:
    """Reemplaza un archivo JSON completo sin dejar un estado parcial."""
    temporary_file = f"{path}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temporary_file, path)


def git_value(repo_dir: str, *args: str) -> str:
    """Obtiene un valor de Git o devuelve una cadena vacía fuera de un repositorio."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def collection_manifest(
    repo_dir: str,
    branch: str,
    collection: str,
    cfg: dict,
    state: dict[str, str],
) -> dict:
    """Construye metadata de procedencia reproducible para una colección."""
    ignore_settings = {
        "exclude_dirs": sorted(cfg["exclude_dirs"]),
        "exclude_ext": sorted(cfg["exclude_ext"]),
        "exclude_files": sorted(cfg["exclude_files"]),
        "valid_ext": sorted(cfg["valid_ext"]),
    }
    ignore_hash = hashlib.sha256(
        json.dumps(ignore_settings, sort_keys=True).encode("utf-8")
    ).hexdigest()
    files = [key.rsplit(":", 1)[0] for key in state]
    file_chunks: dict[str, int] = {}
    languages: dict[str, int] = {}
    for file_path in files:
        file_chunks[file_path] = file_chunks.get(file_path, 0) + 1
        extension = os.path.splitext(file_path)[1]
        language = LANG_MAP.get(extension, "other")
        languages[language] = languages.get(language, 0) + 1

    return {
        "repo": cfg["repo_name"] or os.path.basename(repo_dir),
        "collection": collection,
        "repo_path": repo_dir,
        "branch": branch,
        "git_sha": git_value(repo_dir, "rev-parse", "HEAD"),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "dirty": bool(git_value(repo_dir, "status", "--porcelain")),
        "indexer_version": version("code-context-mcp"),
        "chunk_metadata_version": CHUNK_METADATA_VERSION,
        "embedding_model": cfg["embedding_model"],
        "ignore_hash": ignore_hash,
        "documents": len(state),
        "files_count": len(file_chunks),
        "files": file_chunks,
        "languages": languages,
    }


def save_manifest(branch_dir: str, manifest: dict[str, str | bool]) -> None:
    """Persiste metadata de colección sin modificar configuraciones HNSW inmutables."""
    save_json_atomically(os.path.join(branch_dir, "index_manifest.json"), manifest)


def create_chroma_client(host: str, port: int, auth_token: str) -> chromadb.HttpClient:
    """Crea un cliente HTTP a ChromaDB."""
    settings = Settings(anonymized_telemetry=False)

    kwargs = {"host": host, "port": port, "settings": settings}
    if auth_token:
        kwargs["headers"] = {"Authorization": f"Bearer {auth_token}"}

    client = chromadb.HttpClient(**kwargs)

    try:
        client.heartbeat()
    except Exception as e:
        print(f"❌ No se pudo conectar a ChromaDB en {host}:{port}")
        print(f"   Asegurate de que el servidor esté corriendo: docker compose up -d")
        print(f"   Error: {e}")
        sys.exit(1)

    return client


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Actualiza embeddings delta para una rama (vía ChromaDB HTTP)."
    )
    parser.add_argument("branch", nargs="?", help=BRANCH_HELP)
    parser.add_argument(
        "--repo", default=".",
        help="Ruta al repositorio (para leer .chunking.yaml). Default: directorio actual.",
    )
    parser.add_argument(
        "--chunks-dir", default="./chunks",
        help="Directorio donde se encuentran los JSONL (default: ./chunks).",
    )
    parser.add_argument(
        "--chroma-host", default=None,
        help="Host del servidor ChromaDB (default: localhost).",
    )
    parser.add_argument(
        "--chroma-port", type=int, default=None,
        help="Puerto del servidor ChromaDB (default: 8000).",
    )
    parser.add_argument(
        "--collection-prefix", default=None,
        help="Prefijo para el nombre de la colección (default: 'repo').",
    )
    parser.add_argument(
        "--repo-name", default=None,
        help="Alias del repositorio para buscarlo desde MCP (default: nombre del directorio).",
    )
    parser.add_argument(
        "--current-tree",
        action="store_true",
        help=CURRENT_TREE_HELP,
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo)
    branch = resolve_branch(repo_dir, args.branch, args.current_tree)
    cfg = load_config(repo_dir)

    safe_branch = branch.replace("/", "-").replace("\\", "-")
    chunks_dir = os.path.abspath(args.chunks_dir)
    branch_dir = os.path.join(chunks_dir, safe_branch)

    chroma_host = args.chroma_host or cfg["chroma_host"]
    chroma_port = args.chroma_port or cfg["chroma_port"]
    auth_token = cfg["chroma_auth_token"]
    collection_prefix = args.collection_prefix or cfg["collection_prefix"]
    collection_name = f"{collection_prefix}_{safe_branch}"
    if args.repo_name:
        cfg["repo_name"] = args.repo_name

    print(f"🚀 Iniciando actualización de embeddings para rama '{branch}'")
    print(f"📡 Conectando a ChromaDB en {chroma_host}:{chroma_port}")

    client = create_chroma_client(chroma_host, chroma_port, auth_token)

    # Embeddings server-side con HNSW tuning desde config
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={
            "hnsw:space": cfg["hnsw_space"],
            "hnsw:construction_ef": cfg["hnsw_ef_construction"],
            "hnsw:search_ef": cfg["hnsw_ef_search"],
        },
    )

    changed, deleted_ids, new_state = detect_changes(
        branch_dir, reingest_all=collection.count() == 0
    )

    if not changed and not deleted_ids:
        save_state(branch_dir, new_state)
        save_manifest(
            branch_dir,
            collection_manifest(repo_dir, branch, collection_name, cfg, new_state),
        )
        print("✅ No hay cambios. Nada que actualizar.")
        return

    # Eliminar chunks de archivos borrados
    if deleted_ids:
        print(f"🗑️ Eliminando {len(deleted_ids)} fragmentos obsoletos de '{collection_name}'")
        # ChromaDB acepta delete en batch
        collection.delete(ids=deleted_ids)

    # Upsert chunks nuevos/modificados (en batch para minimizar llamadas HTTP)
    if changed:
        n_batches = (len(changed) + INGEST_BATCH_SIZE - 1) // INGEST_BATCH_SIZE
        print(f"🧠 Insertando {len(changed)} fragmentos en '{collection_name}' ({n_batches} batches)")

        for i in tqdm(range(0, len(changed), INGEST_BATCH_SIZE), desc="Actualizando"):
            batch = changed[i:i + INGEST_BATCH_SIZE]
            collection.upsert(
                ids=[f"{r['file']}-{r['chunk_id']}" for r in batch],
                documents=[r["content"] for r in batch],
                metadatas=[build_metadata(r) for r in batch],
            )

    save_state(branch_dir, new_state)
    save_manifest(
        branch_dir,
        collection_manifest(repo_dir, branch, collection_name, cfg, new_state),
    )

    summary = []
    if changed:
        summary.append(f"{len(changed)} actualizados")
    if deleted_ids:
        summary.append(f"{len(deleted_ids)} eliminados")
    print(f"✅ Ingesta completada: {', '.join(summary)} en '{collection_name}'.")
    print(f"📡 Server: {chroma_host}:{chroma_port}")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"💥 Error inesperado: {e}")
        sys.exit(1)
