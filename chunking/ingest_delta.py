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

from tqdm import tqdm
import chromadb
from chromadb.config import Settings

from chunking.config import load_config


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_text(text: str) -> str:
    """Devuelve un hash MD5 del texto."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


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


def detect_changes(branch_dir: str) -> tuple[list[dict], list[str]]:
    """Detecta fragmentos nuevos/modificados y eliminados.

    Returns:
        (changed, deleted_ids): chunks a upsert y IDs de ChromaDB a eliminar.
    """
    chunks_file = get_last_chunks_file(branch_dir)
    state_file = os.path.join(branch_dir, "last_state.json")

    print(f"🔍 Analizando cambios en {chunks_file}")

    with open(chunks_file, "r", encoding="utf-8") as f:
        curr = [json.loads(line) for line in f]

    if not os.path.exists(state_file):
        print("⚠️ No previous state found. All chunks will be re-ingested.")
        new_state = {f"{c['file']}:{c['chunk_id']}": hash_text(c["content"]) for c in curr}
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(new_state, f, indent=2, ensure_ascii=False)
        return curr, []

    with open(state_file, "r", encoding="utf-8") as f:
        prev = json.load(f)

    changed: list[dict] = []
    new_state: dict[str, str] = {}
    for c in curr:
        key = f"{c['file']}:{c['chunk_id']}"
        h = hash_text(c["content"])
        new_state[key] = h
        if prev.get(key) != h:
            changed.append(c)

    # Detectar chunks eliminados (existían antes pero ya no)
    deleted_keys = set(prev.keys()) - set(new_state.keys())
    deleted_ids = [_state_key_to_chroma_id(k) for k in deleted_keys]

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2, ensure_ascii=False)

    if changed:
        print(f"♻️ {len(changed)} fragmentos modificados o nuevos detectados.")
    if deleted_ids:
        print(f"🗑️ {len(deleted_ids)} fragmentos eliminados detectados.")
    if not changed and not deleted_ids:
        print("✅ No se detectaron cambios en los fragmentos.")

    return changed, deleted_ids


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
    parser.add_argument("branch", nargs="?", help="Nombre de la rama Git a procesar.")
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
    args = parser.parse_args()

    if not args.branch:
        print("❌ Error: debes especificar el nombre de la rama.\n")
        print("Uso: chunking-ingest <nombre_rama>")
        sys.exit(1)

    repo_dir = os.path.abspath(args.repo)
    cfg = load_config(repo_dir)

    safe_branch = args.branch.replace("/", "-").replace("\\", "-")
    chunks_dir = os.path.abspath(args.chunks_dir)
    branch_dir = os.path.join(chunks_dir, safe_branch)

    chroma_host = args.chroma_host or cfg["chroma_host"]
    chroma_port = args.chroma_port or cfg["chroma_port"]
    auth_token = cfg["chroma_auth_token"]
    collection_prefix = args.collection_prefix or cfg["collection_prefix"]
    collection_name = f"{collection_prefix}_{safe_branch}"

    print(f"🚀 Iniciando actualización de embeddings para rama '{args.branch}'")
    print(f"📡 Conectando a ChromaDB en {chroma_host}:{chroma_port}")

    client = create_chroma_client(chroma_host, chroma_port, auth_token)

    # No se especifica embedding_function — ChromaDB usa su default server-side
    collection = client.get_or_create_collection(name=collection_name)

    changed, deleted_ids = detect_changes(branch_dir)

    if not changed and not deleted_ids:
        print("✅ No hay cambios. Nada que actualizar.")
        return

    # Eliminar chunks de archivos borrados
    if deleted_ids:
        print(f"🗑️ Eliminando {len(deleted_ids)} fragmentos obsoletos de '{collection_name}'")
        # ChromaDB acepta delete en batch
        collection.delete(ids=deleted_ids)

    # Upsert chunks nuevos/modificados
    if changed:
        print(f"🧠 Insertando {len(changed)} fragmentos en '{collection_name}' (embeddings server-side)")
        for rec in tqdm(changed, desc="Actualizando"):
            uid = f"{rec['file']}-{rec['chunk_id']}"
            collection.upsert(
                ids=[uid],
                documents=[rec["content"]],
                metadatas=[{
                    "file": rec["file"],
                    "chunk_id": rec["chunk_id"],
                    "tokens": rec["tokens"],
                }],
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
