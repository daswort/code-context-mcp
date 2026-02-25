"""
MCP Server para búsqueda semántica de código en ChromaDB.

Expone tools que permiten a AI assistants (Antigravity, Cursor, VS Code, etc.)
buscar código relevante en las colecciones indexadas.

Uso:
    chunking-mcp                           # stdio (default, para AI clients)
    chunking-mcp --transport sse           # SSE (para depuración)
"""

import os
import argparse

import chromadb
from chromadb.config import Settings
from mcp.server.fastmcp import FastMCP

# ─── Server ──────────────────────────────────────────────────────────────────

mcp = FastMCP("code-context-mcp")

# Configuración por variables de entorno (simple para MCP)
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
CHROMA_AUTH_TOKEN = os.environ.get("CHROMA_AUTH_TOKEN", "")


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

@mcp.tool()
def search_code(query: str, collection: str, n_results: int = 5) -> str:
    """
    Busca fragmentos de código relevantes en una colección de ChromaDB.

    Args:
        query: Texto de búsqueda (ej: "autenticación JWT", "validar permisos")
        collection: Nombre de la colección (ej: "agenda2-app_main")
        n_results: Cantidad de resultados a devolver (default: 5)

    Returns:
        Fragmentos de código relevantes con metadata (archivo, chunk_id)
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe. Usa list_collections() para ver las disponibles."

    results = col.query(query_texts=[query], n_results=n_results)

    if not results["documents"] or not results["documents"][0]:
        return "No se encontraron resultados para la búsqueda."

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        file_path = meta.get("file", "desconocido")
        chunk_id = meta.get("chunk_id", "?")
        score = round(1 - dist, 4) if dist is not None else "N/A"
        output.append(
            f"📄 **{file_path}** (chunk {chunk_id}, score: {score})\n"
            f"```\n{doc}\n```"
        )

    return "\n\n---\n\n".join(output)


@mcp.tool()
def list_collections() -> str:
    """
    Lista todas las colecciones disponibles en ChromaDB con su cantidad de documentos.

    Returns:
        Lista de colecciones con nombre y cantidad de documentos
    """
    client = _get_client()
    collections = client.list_collections()

    if not collections:
        return "No hay colecciones. Ejecuta `chunking-get` y `chunking-ingest` para crear una."

    lines = []
    for col in collections:
        count = client.get_collection(col.name).count()
        lines.append(f"  • **{col.name}** — {count} fragmentos")

    return f"📚 {len(collections)} colecciones:\n\n" + "\n".join(lines)


@mcp.tool()
def get_collection_info(collection: str) -> str:
    """
    Obtiene información detallada de una colección: cantidad de documentos,
    archivos indexados y distribución por archivo.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")

    Returns:
        Info detallada de la colección
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe."

    count = col.count()
    if count == 0:
        return f"La colección '{collection}' está vacía."

    # Obtener todos los metadatos para estadísticas
    all_data = col.get(include=["metadatas"])
    files: dict[str, int] = {}
    for meta in all_data["metadatas"]:
        f = meta.get("file", "desconocido")
        files[f] = files.get(f, 0) + 1

    lines = [f"📊 **{collection}** — {count} fragmentos de {len(files)} archivos\n"]
    for file_path, n in sorted(files.items(), key=lambda x: -x[1]):
        lines.append(f"  • {file_path} ({n} chunks)")

    return "\n".join(lines)


@mcp.tool()
def get_file_chunks(collection: str, file_path: str) -> str:
    """
    Obtiene todos los fragmentos de un archivo específico dentro de una colección.
    Útil para leer el código completo de un archivo indexado.

    Args:
        collection: Nombre de la colección (ej: "agenda2-app_main")
        file_path: Ruta del archivo (ej: "backend/internal/config/config.go")

    Returns:
        Todos los chunks del archivo, ordenados por chunk_id
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe."

    results = col.get(
        where={"file": file_path},
        include=["documents", "metadatas"],
    )

    if not results["documents"]:
        # Intentar búsqueda parcial por si la ruta no es exacta
        all_data = col.get(include=["metadatas"])
        matching = [m["file"] for m in all_data["metadatas"] if file_path in m.get("file", "")]
        if matching:
            unique = sorted(set(matching))
            return (
                f"No se encontró '{file_path}' exacto. Archivos similares:\n"
                + "\n".join(f"  • {f}" for f in unique[:10])
            )
        return f"No se encontraron chunks para '{file_path}' en '{collection}'."

    # Ordenar por chunk_id
    pairs = sorted(
        zip(results["documents"], results["metadatas"]),
        key=lambda x: x[1].get("chunk_id", 0),
    )

    output = [f"📄 **{file_path}** — {len(pairs)} chunks\n"]
    for doc, meta in pairs:
        chunk_id = meta.get("chunk_id", "?")
        output.append(f"### Chunk {chunk_id}\n```\n{doc}\n```")

    return "\n\n".join(output)


@mcp.tool()
def peek_collection(collection: str, limit: int = 5) -> str:
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
    limit = min(limit, 20)

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe."

    results = col.peek(limit=limit)

    if not results["documents"]:
        return f"La colección '{collection}' está vacía."

    output = [f"👁️ Vista previa de '{collection}' ({col.count()} total, mostrando {len(results['documents'])})\n"]
    for uid, doc, meta in zip(results["ids"], results["documents"], results["metadatas"]):
        file_path = meta.get("file", "?")
        chunk_id = meta.get("chunk_id", "?")
        preview = doc[:200] + "..." if len(doc) > 200 else doc
        output.append(f"**{file_path}** (chunk {chunk_id})\n```\n{preview}\n```")

    return "\n\n---\n\n".join(output)


@mcp.tool()
def delete_collection(collection: str) -> str:
    """
    Elimina una colección completa de ChromaDB.
    ⚠️ Esta acción es irreversible.

    Args:
        collection: Nombre de la colección a eliminar

    Returns:
        Confirmación de la eliminación
    """
    client = _get_client()

    try:
        client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe."

    client.delete_collection(name=collection)
    return f"🗑️ Colección '{collection}' eliminada correctamente."


@mcp.tool()
def get_document(collection: str, document_id: str) -> str:
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
        return f"❌ La colección '{collection}' no existe."

    results = col.get(ids=[document_id], include=["documents", "metadatas"])

    if not results["documents"]:
        return f"No se encontró el documento con ID '{document_id}'."

    doc = results["documents"][0]
    meta = results["metadatas"][0]
    file_path = meta.get("file", "?")
    chunk_id = meta.get("chunk_id", "?")

    return f"📄 **{file_path}** (chunk {chunk_id}, ID: {document_id})\n```\n{doc}\n```"


@mcp.tool()
def search_by_file_pattern(collection: str, pattern: str, n_results: int = 10) -> str:
    """
    Busca archivos indexados cuyo path contenga un patrón dado.
    Útil para descubrir qué archivos están indexados.

    Args:
        collection: Nombre de la colección
        pattern: Patrón a buscar en los paths (ej: "config", "auth", ".go")
        n_results: Máximo de archivos a mostrar (default: 10)

    Returns:
        Lista de archivos que coinciden con el patrón
    """
    client = _get_client()

    try:
        col = client.get_collection(name=collection)
    except Exception:
        return f"❌ La colección '{collection}' no existe."

    all_data = col.get(include=["metadatas"])
    files: dict[str, int] = {}
    for meta in all_data["metadatas"]:
        f = meta.get("file", "")
        if pattern.lower() in f.lower():
            files[f] = files.get(f, 0) + 1

    if not files:
        return f"No se encontraron archivos que contengan '{pattern}' en '{collection}'."

    sorted_files = sorted(files.items(), key=lambda x: -x[1])[:n_results]
    lines = [f"🔍 {len(files)} archivos contienen '{pattern}':\n"]
    for file_path, n in sorted_files:
        lines.append(f"  • {file_path} ({n} chunks)")

    if len(files) > n_results:
        lines.append(f"\n  ... y {len(files) - n_results} más")

    return "\n".join(lines)


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
