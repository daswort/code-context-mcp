# 🧩 code-context-mcp

Pipeline para segmentar código fuente en chunks, generar embeddings y almacenarlos en ChromaDB. Diseñado para dar contexto semántico a AI coding assistants (Antigravity, Cursor, VS Code Copilot) vía **Model Context Protocol (MCP)**.

## Arquitectura

```
┌────────────────────────────────────────────────────────┐
│  AI Assistant (Antigravity, Cursor, VS Code, etc.)     │
│  Usa tools MCP para buscar código relevante            │
└────────────────────┬───────────────────────────────────┘
                     │ MCP (stdio)
┌────────────────────▼───────────────────────────────────┐
│  chunking-mcp                                          │
│  MCP Server liviano · 8 tools disponibles              │
│  Sin modelo local — queries vía HTTP                   │
└────────────────────┬───────────────────────────────────┘
                     │ HTTP
┌────────────────────▼───────────────────────────────────┐
│  ChromaDB (Docker)                                     │
│  Genera embeddings server-side (all-MiniLM-L6-v2)      │
│  Almacena y busca vectores                             │
│  Named volume: code-context-chroma-data                │
└────────────────────────────────────────────────────────┘
```

## Instalación

```bash
# Clonar
git clone git@github.com:daswort/code-context-mcp.git && cd code-context-mcp

# Crear virtualenv e instalar
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Levantar ChromaDB
cd docker && docker compose up -d
```

Esto instala 3 comandos CLI:

| Comando | Función |
|---------|---------|
| `chunking-get` | Segmenta código fuente en chunks JSONL |
| `chunking-ingest` | Ingesta chunks en ChromaDB (delta incremental) |
| `chunking-mcp` | MCP server para AI assistants |

## Uso rápido

### 1. Segmentar código

```bash
chunking-get <rama> --repo /ruta/al/repo --output /ruta/chunks/mi-repo
```

Ejemplo:
```bash
chunking-get main --repo ~/projects/agenda2-app --output ~/projects/chunking/chunks/agenda2-app
```

Esto genera un archivo JSONL con todos los fragmentos del código fuente:
```
chunks/agenda2-app/main/00001_chunks.jsonl
```

### 2. Ingestar en ChromaDB

```bash
chunking-ingest <rama> --repo /ruta/al/repo --chunks-dir /ruta/chunks/mi-repo
```

Ejemplo:
```bash
chunking-ingest main --repo ~/projects/agenda2-app --chunks-dir ~/projects/chunking/chunks/agenda2-app
```

La ingesta es **delta**: detecta chunks nuevos, modificados y eliminados. Solo re-procesa lo necesario:

```
🔍 Analizando cambios...
♻️ 3 fragmentos modificados o nuevos detectados.
🗑️ 1 fragmentos eliminados detectados.
🧠 Insertando 3 fragmentos (embeddings server-side)
🗑️ Eliminando 1 fragmentos obsoletos
✅ Ingesta completada: 3 actualizados, 1 eliminados en 'agenda2-app_main'.
```

### 3. Buscar código (MCP)

El MCP server expone 8 tools para AI assistants:

| Tool | Descripción |
|------|-------------|
| `search_code` | Búsqueda semántica por query de texto libre |
| `list_collections` | Lista todas las colecciones con conteos |
| `get_collection_info` | Distribución de archivos y chunks por colección |
| `get_file_chunks` | Todos los chunks de un archivo específico |
| `peek_collection` | Vista previa de los primeros N documentos |
| `delete_collection` | Eliminar una colección completa |
| `get_document` | Obtener un chunk específico por ID |
| `search_by_file_pattern` | Buscar archivos indexados por patrón en el path |

### 4. Preview sin procesar (dry-run)

```bash
chunking-get main --repo ~/projects/mi-repo --dry-run
```

Lista los archivos que se procesarían, agrupados por extensión, sin ejecutar nada:

```
🔎 Dry-run para repo '/home/user/projects/mi-repo' (rama 'main')

📋 Dry-run: 42 archivos serían procesados

  .go  (25 archivos)
    • backend/cmd/api/main.go
    • backend/internal/config/auth.go
    ...

  .sql  (12 archivos)
    • backend/migrations/000001_init_extensions.up.sql
    ...

Total: 42 archivos
```

## Configuración por proyecto

Cada repositorio puede tener un archivo `.chunking.yaml` en su raíz para personalizar el comportamiento. Las listas **extienden** los defaults, no los reemplazan.

Copie `.chunking.example.yaml` como punto de partida:
```bash
cp /ruta/a/chunking/.chunking.example.yaml ~/projects/mi-repo/.chunking.yaml
```

### Ejemplo completo

```yaml
# ~/projects/mi-repo/.chunking.yaml

# Directorios adicionales a excluir
exclude_dirs:
  - vendor
  - tmp

# Extensiones adicionales a excluir
exclude_ext:
  - .log

# Extensiones adicionales a incluir
extra_valid_ext:
  - .go
  - .py
  - .html
  - .sql

# Parámetros de chunking
chunk_size: 800
chunk_overlap: 100

# ChromaDB
collection_prefix: mi-proyecto     # → colección: mi-proyecto_main
chroma_host: localhost
chroma_port: 8000
# chroma_auth_token: mi-token     # Opcional
```

### Defaults incluidos

<details>
<summary>📁 Directorios excluidos por defecto</summary>

`.git`, `__pycache__`, `node_modules`, `dist`, `build`, `.venv`, `.idea`, `.vscode`, `.github`, `bin`, `obj`, `chunks`
</details>

<details>
<summary>🚫 Extensiones excluidas por defecto</summary>

`.exe`, `.bin`, `.dll`, `.pdb`, `.user`, `.jpg`, `.jpeg`, `.png`, `.gif`, `.zip`, `.tar`, `.gz`, `.lock`
</details>

<details>
<summary>✅ Extensiones válidas por defecto</summary>

`.cs`, `.csproj`, `.sln`, `.cshtml`, `.js`, `.ts`, `.md`, `.json`, `.yaml`, `.yml`, `.txt`, `.http`
</details>

## Configuración MCP para AI Assistants

### Antigravity

Agregar a `~/.gemini/antigravity/mcp_config.json`:

```json
{
  "mcpServers": {
    "code-context": {
      "command": "/ruta/a/code-context-mcp/.venv/bin/chunking-mcp",
      "env": {
        "CHROMA_HOST": "localhost",
        "CHROMA_PORT": "8000"
      }
    }
  }
}
```

### Cursor

Agregar a `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "code-context": {
      "command": "/ruta/a/code-context-mcp/.venv/bin/chunking-mcp",
      "env": {
        "CHROMA_HOST": "localhost",
        "CHROMA_PORT": "8000"
      }
    }
  }
}
```

### VS Code (Copilot)

Agregar a `.vscode/mcp.json`:

```json
{
  "servers": {
    "code-context": {
      "type": "stdio",
      "command": "/ruta/a/code-context-mcp/.venv/bin/chunking-mcp",
      "env": {
        "CHROMA_HOST": "localhost",
        "CHROMA_PORT": "8000"
      }
    }
  }
}
```

## ChromaDB (Docker)

El servidor se levanta con Docker Compose:

```bash
cd docker/
docker compose up -d
```

### Configuración

Editar `docker/.env`:

```env
# Puerto (default: 8000)
CHROMA_PORT=8000

# Autenticación por token (opcional)
# CHROMA_AUTH_TOKEN=mi-token-secreto
# CHROMA_AUTH_PROVIDER=chromadb.auth.token_authn.TokenAuthenticationServerProvider
```

### Operaciones comunes

```bash
# Ver estado
docker compose ps

# Ver logs
docker compose logs -f chromadb

# Parar
docker compose down

# Parar y borrar datos
docker compose down -v
```

Los datos persisten en un named volume (`code-context-chroma-data`). Se mantienen entre reinicios del contenedor.

## Script de orquestación

`run_branch_tasks.sh` ejecuta el pipeline completo (get + ingest) para la rama actual o una específica:

```bash
# Rama actual
./run_branch_tasks.sh

# Rama específica
./run_branch_tasks.sh feature/mi-feature
```

## Estructura del proyecto

```
code-context-mcp/
├── .chunking.example.yaml       # Template de configuración
├── pyproject.toml                # Paquete Python (v1.0.0)
├── run_branch_tasks.sh           # Orquestador get + ingest
├── docker/
│   ├── docker-compose.yml        # ChromaDB server
│   └── .env                      # Variables de entorno
└── chunking/
    ├── __init__.py
    ├── config.py                 # Defaults + merge con .chunking.yaml
    ├── get_chunks.py             # chunking-get: segmentación de código
    ├── ingest_delta.py           # chunking-ingest: ingesta delta en ChromaDB
    └── mcp_server.py             # chunking-mcp: MCP server (8 tools)
```

## Requisitos

- Python ≥ 3.10
- Docker (para ChromaDB)
- Git (los repos a procesar deben ser repositorios Git)
