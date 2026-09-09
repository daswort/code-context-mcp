"""
MCP Server para búsqueda semántica de código en ChromaDB.

Expone tools que permiten a AI assistants (Antigravity, Cursor, VS Code, etc.)
buscar código relevante en las colecciones indexadas.

Uso:
    chunking-mcp                           # stdio (default, para AI clients)
    chunking-mcp --transport sse           # SSE (para depuración)
"""

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
from mcp.server.fastmcp import FastMCP

# ─── Server ──────────────────────────────────────────────────────────────────

mcp = FastMCP("code-context-mcp")

# Configuración por variables de entorno (simple para MCP)
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
CHROMA_AUTH_TOKEN = os.environ.get("CHROMA_AUTH_TOKEN", "")
CHUNKS_DIR = Path(os.environ.get("CODE_CONTEXT_CHUNKS_DIR", Path(__file__).parent.parent / "chunks"))
MAX_RESULTS = 10
MAX_SNIPPET_CHARS = 4_000
MAX_TOTAL_CHARS = 12_000
MAX_DOCUMENTS_PER_RESPONSE = MAX_TOTAL_CHARS // MAX_SNIPPET_CHARS
INDEX_WARNING = "Semantic index results are discovery aids; verify against the real file before citing."


def _get_client() -> chromadb.HttpClient:
    """Crea y devuelve un cliente HTTP a ChromaDB."""
    kwargs = {
        "host": CHROMA_HOST,
        "port": CHROMA_PORT,
        "settings": Settings(anonymized_telemetry=False),
    }
    if CHROMA_AUTH_TOKEN:
        kwargs["headers"] = {"Authorization": f"Bearer {CHROMA_AUTH_TOKEN}"}
    return chromadb.HttpClient(**kwargs)


# ─── Tools ───────────────────────────────────────────────────────────────────

def _build_where_filter(**conditions: str | None) -> dict | None:
    """Construye un filtro `where` a partir de condiciones opcionales."""
    parts = []
    for field, value in conditions.items():
        if value is not None:
            parts.append({field: value})
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"$and": parts}


def _limit(value: int, maximum: int = MAX_RESULTS) -> int:
    return max(1, min(value, maximum))


def _load_manifests() -> dict[str, dict[str, Any]]:
    """Carga manifests locales sin consultar ni recorrer los documentos de Chroma."""
    manifests: dict[str, dict[str, Any]] = {}
    if not CHUNKS_DIR.exists():
        return manifests

    for path in CHUNKS_DIR.rglob("index_manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            collection = manifest.get("collection")
            if isinstance(collection, str):
                manifests[collection] = manifest
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def _git_value(repo_path: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _freshness(manifest: dict[str, Any] | None) -> str:
    if not manifest or not manifest.get("repo_path") or not manifest.get("git_sha"):
        return "unknown"
    repo_path = manifest["repo_path"]
    if not os.path.isdir(repo_path):
        return "unknown"
    if manifest.get("dirty") or _git_value(repo_path, "status", "--porcelain"):
        return "stale"
    if _git_value(repo_path, "rev-parse", "HEAD") != manifest["git_sha"]:
        return "stale"
    if _git_value(repo_path, "branch", "--show-current") != manifest.get("branch"):
        return "stale"
    return "ok"


def _freshness_details(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Explica el estado de freshness sin convertir un índice stale en inutilizable."""
    if not manifest or not manifest.get("repo_path") or not manifest.get("git_sha"):
        return {"freshness": "unknown", "reasons": ["manifest_incomplete"]}

    repo_path = manifest["repo_path"]
    if not os.path.isdir(repo_path):
        return {"freshness": "unknown", "reasons": ["repo_path_unavailable"]}

    head_changed = _git_value(repo_path, "rev-parse", "HEAD") != manifest["git_sha"]
    branch_changed = _git_value(repo_path, "branch", "--show-current") != manifest.get("branch")
    dirty_now = bool(_git_value(repo_path, "status", "--porcelain"))
    reasons = []
    if manifest.get("dirty"):
        reasons.append("dirty_at_index")
    if dirty_now:
        reasons.append("dirty_now")
    if head_changed:
        reasons.append("head_changed")
    if branch_changed:
        reasons.append("branch_changed")
    return {
        "freshness": "stale" if reasons else "ok",
        "dirty_at_index": bool(manifest.get("dirty")),
        "dirty_now": dirty_now,
        "head_changed": head_changed,
        "branch_changed": branch_changed,
        "reasons": reasons,
    }


def _collection_context(collection: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = _load_manifests().get(collection) if manifest is None else manifest
    return {
        "collection": collection,
        "repo": manifest.get("repo") if manifest else None,
        "branch": manifest.get("branch") if manifest else None,
        "git_sha": manifest.get("git_sha") if manifest else None,
        "indexed_at": manifest.get("indexed_at") if manifest else None,
        "freshness": _freshness(manifest),
    }


def _repo_root(manifest: dict[str, Any] | None) -> str | None:
    """Absolute repo root the collection was indexed from; `None` when the manifest cannot say."""
    root = manifest.get("repo_path") if manifest else None
    return root.rstrip("/") if isinstance(root, str) and root else None


def _public_path(path: Any, root: str | None) -> Any:
    """Repo-relative form of an indexed path: the only form an answer, ticket or PR may quote.
    Returned unchanged when the root is unknown or the path lies outside it."""
    if not isinstance(path, str) or not root:
        return path
    prefix = f"{root}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _stored_path(path: str, root: str | None) -> str:
    """The absolute form the index stores, so a caller may pass either form back."""
    if not root or path.startswith("/"):
        return path
    return f"{root}/{path}"


def _error(message: str, hint: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"error": message}
    if hint:
        response["hint"] = hint
    return response


def _result_items(results: dict[str, Any], context: dict[str, Any],
                  root: str | None = None) -> list[dict[str, Any]]:
    items = []
    remaining = MAX_TOTAL_CHARS
    for document, metadata, distance in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        if remaining <= 0:
            break
        snippet = document[:min(MAX_SNIPPET_CHARS, remaining)]
        remaining -= len(snippet)
        items.append({
            "file": _public_path(metadata.get("file"), root),
            "abs_path": metadata.get("file"),
            "chunk_id": metadata.get("chunk_id"),
            "score": round(1 - distance, 4) if distance is not None else None,
            "start_line": metadata.get("start_line"),
            "end_line": metadata.get("end_line"),
            "symbol": metadata.get("symbol"),
            "snippet": snippet,
            "collection_git_sha": context["git_sha"],
        })
    return items


@mcp.tool()
def search_code(
    query: str,
    collection: str,
    n_results: int = 5,
    file_ext: str | None = None,
    language: str | None = None,
    contains: str | None = None,
) -> dict[str, Any]:
    """
    Busca fragmentos de código relevantes en una colección de ChromaDB.
    Soporta filtros opcionales por extensión, lenguaje o contenido exacto.

    Args:
        query: Texto de búsqueda (ej: "autenticación JWT", "validar permisos")
        collection: Nombre de la colección (ej: "agenda2-app_main")
        n_results: Cantidad de resultados a devolver (default: 5)
        file_ext: Filtrar por extensión (ej: ".go", ".py", ".ts")
        language: Filtrar por lenguaje (ej: "python", "go", "typescript")
        contains: Texto exacto que debe estar en el fragmento (ej: nombre de función)

    Returns:
        Resultado estructurado y limitado para uso por agentes
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found", "Use list_collections to see available collections.")

    where = _build_where_filter(ext=file_ext, language=language)
    where_doc = {"$contains": contains} if contains else None

    results = col.query(
        query_texts=[query],
        n_results=_limit(n_results),
        where=where,
        where_document=where_doc,
    )

    manifest = _load_manifests().get(collection)
    context = _collection_context(collection, manifest)

    if not results["documents"] or not results["documents"][0]:
        return {**context, "results": [], "warnings": [INDEX_WARNING]}

    items = _result_items(results, context, _repo_root(manifest))
    return {**context, "results": items, "warnings": [INDEX_WARNING]}


@mcp.tool()
def list_collections() -> dict[str, Any]:
    """
    Lista todas las colecciones disponibles en ChromaDB con su cantidad de documentos.

    Returns:
        Lista de colecciones con nombre y cantidad de documentos
    """
    client = _get_client()
    collections = client.list_collections()

    manifests = _load_manifests()
    return {
        "collections": [
            {
                "collection": col.name,
                "repo": manifests.get(col.name, {}).get("repo"),
                "branch": manifests.get(col.name, {}).get("branch"),
                "documents": manifests.get(col.name, {}).get("documents"),
                "freshness": _freshness(manifests.get(col.name)),
            }
            for col in collections
        ]
    }


@mcp.tool()
def get_collection_summary(collection: str) -> dict[str, Any]:
    """
    Obtiene el resumen liviano de una colección sin recorrer todos sus chunks.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")

    Returns:
        Metadata de procedencia y estadísticas precalculadas
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found")

    manifest = _load_manifests().get(collection, {})
    return {
        **_collection_context(collection),
        "documents": manifest.get("documents", col.count()),
        "files_count": manifest.get("files_count"),
        "languages": manifest.get("languages"),
        "embedding_model": manifest.get("embedding_model"),
        "warnings": [] if manifest else ["Collection was indexed before manifests were introduced."],
    }


@mcp.tool()
def get_file_chunks(collection: str, file_path: str, limit: int = 10, offset: int = 0) -> dict[str, Any]:
    """
    Obtiene todos los fragmentos de un archivo específico dentro de una colección.
    Útil para leer el código completo de un archivo indexado.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")
        file_path: Ruta del archivo (ej: "backend/internal/config/config.go")

    Returns:
        Página de chunks del archivo, ordenada por chunk_id
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found")

    manifest = _load_manifests().get(collection)
    root = _repo_root(manifest)
    stored = _stored_path(file_path, root)

    page_limit = _limit(limit, MAX_DOCUMENTS_PER_RESPONSE)
    results = col.get(
        where={"file": stored},
        include=["documents", "metadatas"],
        limit=page_limit,
        offset=max(0, offset),
    )

    if not results["documents"]:
        return {**_collection_context(collection, manifest), "file": _public_path(stored, root),
                "abs_path": stored, "chunks": [], "next_offset": None}

    # Ordenar por chunk_id
    pairs = sorted(
        zip(results["documents"], results["metadatas"]),
        key=lambda x: x[1].get("chunk_id", 0),
    )

    chunks = []
    remaining = MAX_TOTAL_CHARS
    for document, metadata in pairs:
        if remaining <= 0:
            break
        content = document[:min(MAX_SNIPPET_CHARS, remaining)]
        remaining -= len(content)
        chunks.append({
            "chunk_id": metadata.get("chunk_id"),
            "start_line": metadata.get("start_line"),
            "end_line": metadata.get("end_line"),
            "content": content,
        })

    return {
        **_collection_context(collection, manifest),
        "file": _public_path(stored, root),
        "abs_path": stored,
        "chunks": chunks,
        "ordering": "chunk_id_within_page",
        "next_offset": offset + len(pairs) if len(pairs) == page_limit else None,
        "warnings": [INDEX_WARNING],
    }


@mcp.tool()
def peek_collection(collection: str, limit: int = 5) -> dict[str, Any]:
    """
    Muestra una vista previa de los primeros documentos de una colección.
    Útil para entender qué tipo de contenido tiene una colección.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")
        limit: Cantidad de documentos a mostrar (default: 5, max: 20)

    Returns:
        Vista previa de los primeros documentos con su metadata
    """
    client = _get_client()
    limit = _limit(limit, MAX_DOCUMENTS_PER_RESPONSE)

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found")

    results = col.peek(limit=limit)
    manifest = _load_manifests().get(collection)
    root = _repo_root(manifest)

    if not results["documents"]:
        return {**_collection_context(collection, manifest), "documents": [], "warnings": [INDEX_WARNING]}

    remaining = MAX_TOTAL_CHARS
    documents = []
    for document, metadata in zip(results["documents"], results["metadatas"]):
        if remaining <= 0:
            break
        snippet = document[:min(MAX_SNIPPET_CHARS, remaining)]
        remaining -= len(snippet)
        documents.append({
            "file": _public_path(metadata.get("file"), root),
            "chunk_id": metadata.get("chunk_id"),
            "snippet": snippet,
        })
    return {**_collection_context(collection, manifest), "documents": documents,
            "warnings": [INDEX_WARNING]}


@mcp.tool()
def get_document(collection: str, document_id: str) -> dict[str, Any]:
    """
    Obtiene un documento específico por su ID.

    Args:
        collection: Nombre de la colección
        document_id: ID del documento (formato: "archivo-chunk_id", ej: "./src/main.go-0")

    Returns:
        Contenido del documento con su metadata
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found")

    results = col.get(ids=[document_id], include=["documents", "metadatas"])

    if not results["documents"]:
        return _error("document_not_found")

    doc = results["documents"][0]
    meta = results["metadatas"][0]
    manifest = _load_manifests().get(collection)
    return {
        **_collection_context(collection, manifest),
        "document": {
            "id": document_id,
            "file": _public_path(meta.get("file"), _repo_root(manifest)),
            "abs_path": meta.get("file"),
            "chunk_id": meta.get("chunk_id"),
            "start_line": meta.get("start_line"),
            "end_line": meta.get("end_line"),
            "content": doc[:MAX_SNIPPET_CHARS],
        },
        "warnings": [INDEX_WARNING],
    }


@mcp.tool()
def search_by_file_pattern(collection: str, pattern: str, n_results: int = 10) -> dict[str, Any]:
    """
    Busca archivos indexados cuyo path contenga un patrón dado.
    Útil para descubrir qué archivos están indexados.

    Usa el índice de paths del manifest local, sin recorrer chunks en ChromaDB.

    Args:
        collection: Nombre de la colección
        pattern: Patrón a buscar en los paths (ej: "config", "auth", ".go")
        n_results: Máximo de archivos a mostrar (default: 10)

    Returns:
        Lista de archivos que coinciden con el patrón
    """
    manifest = _load_manifests().get(collection)
    if not manifest:
        return _error("manifest_not_found", "Run chunking-ingest to create a file index.")
    files = manifest.get("files")
    if not isinstance(files, dict):
        return _error("file_index_not_found", "Run chunking-ingest to refresh the manifest.")

    root = _repo_root(manifest)
    normalized_pattern = pattern.lower()
    matches = [
        {"file": _public_path(file_path, root), "chunks": chunk_count}
        for file_path, chunk_count in files.items()
        if normalized_pattern in file_path.lower()
    ]
    matches.sort(key=lambda item: (-item["chunks"], item["file"]))
    limit = _limit(n_results)
    return {
        **_collection_context(collection, manifest),
        "pattern": pattern,
        "files": matches[:limit],
        "total_matches": len(matches),
        "truncated": len(matches) > limit,
        "warnings": [],
    }


@mcp.tool()
def search_exact(
    collection: str,
    contains: str | None = None,
    regex: str | None = None,
    file_ext: str | None = None,
    language: str | None = None,
    n_results: int = 10,
) -> dict[str, Any]:
    """
    Busca fragmentos de código por texto exacto o expresión regular.
    No usa búsqueda semántica — ideal para encontrar símbolos, funciones,
    imports o patrones de código específicos.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")
        contains: Texto exacto a buscar (ej: "func handleAuth", "import React")
        regex: Expresión regular a buscar (ej: "def \\w+_handler", "class \\w+Service")
        file_ext: Filtrar por extensión (ej: ".go", ".py")
        language: Filtrar por lenguaje (ej: "python", "typescript")
        n_results: Máximo de resultados (default: 10)

    Returns:
        Fragmentos que coinciden con el texto o regex
    """
    if not contains and not regex:
        return _error("missing_search_term", "Specify contains or regex.")

    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return _error("collection_not_found")

    # Filtro por contenido del documento
    doc_conditions = []
    if contains:
        doc_conditions.append({"$contains": contains})
    if regex:
        doc_conditions.append({"$regex": regex})

    where_doc = doc_conditions[0] if len(doc_conditions) == 1 else {"$and": doc_conditions}

    # Filtro por metadata
    where = _build_where_filter(ext=file_ext, language=language)

    results = col.get(
        where=where,
        where_document=where_doc,
        include=["documents", "metadatas"],
        limit=_limit(n_results),
    )

    manifest = _load_manifests().get(collection)
    context = _collection_context(collection, manifest)
    root = _repo_root(manifest)
    items = []
    remaining = MAX_TOTAL_CHARS
    for document, metadata in zip(results["documents"], results["metadatas"]):
        if remaining <= 0:
            break
        snippet = document[:min(MAX_SNIPPET_CHARS, remaining)]
        remaining -= len(snippet)
        items.append({
            "file": _public_path(metadata.get("file"), root),
            "abs_path": metadata.get("file"),
            "chunk_id": metadata.get("chunk_id"),
            "start_line": metadata.get("start_line"),
            "end_line": metadata.get("end_line"),
            "symbol": metadata.get("symbol"),
            "snippet": snippet,
            "collection_git_sha": context["git_sha"],
        })
    return {**context, "results": items, "warnings": [INDEX_WARNING]}


def _resolve_repo(repo: str, branch: str | None = None) -> dict[str, Any] | None:
    matches = [
        manifest for manifest in _load_manifests().values()
        if manifest.get("repo") == repo and (branch is None or manifest.get("branch") == branch)
    ]
    return matches[0] if len(matches) == 1 else None


@mcp.tool()
def search_repo(
    repo: str,
    query: str,
    branch: str | None = None,
    n_results: int = 5,
    file_ext: str | None = None,
    language: str | None = None,
    contains: str | None = None,
) -> dict[str, Any]:
    """Busca código por alias de repositorio, sin que el agente conozca la colección."""
    manifest = _resolve_repo(repo, branch)
    if not manifest:
        return _error("repo_not_found_or_ambiguous", "Specify a branch or run chunking-ingest to create a manifest.")
    return search_code(
        query=query,
        collection=manifest["collection"],
        n_results=n_results,
        file_ext=file_ext,
        language=language,
        contains=contains,
    )


@mcp.tool()
def status(repo: str, branch: str | None = None) -> dict[str, Any]:
    """Devuelve el estado de freshness de un repositorio indexado."""
    manifest = _resolve_repo(repo, branch)
    if not manifest:
        return _error("repo_not_found_or_ambiguous", "Specify a branch or run chunking-ingest to create a manifest.")
    return {**_collection_context(manifest["collection"]), **_freshness_details(manifest)}


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server para búsqueda de código.")
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio",
        help="Transporte MCP (default: stdio).",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
