"""
Segmenta el código fuente de un repositorio en chunks y los guarda como JSONL.

Uso:
    chunking-get <branch> [--repo .] [--output ./chunks] [--dry-run]
    chunking-get --current-tree [--repo .] [--output ./chunks] [--dry-run]

`<branch>` debe ser la rama activa del repo: el indexador no hace checkout ni pull, y falla con
instrucciones accionables si no coincide. `--current-tree` indexa la rama activa y no necesita el
nombre; si se pasa uno de todos modos, lo ignora.
"""

import os
import json
import argparse

from langchain_text_splitters import RecursiveCharacterTextSplitter

from chunking.config import load_config
from chunking.git_state import resolve_branch


# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_valid_file(path: str, cfg: dict) -> bool:
    filename = os.path.basename(path)
    parts = set(os.path.normpath(path).split(os.sep))

    if filename in cfg["exclude_files"]:
        return False
    if parts & cfg["exclude_dirs"]:
        return False
    ext = os.path.splitext(filename)[1]
    if ext in cfg["exclude_ext"]:
        return False
    if ext not in cfg["valid_ext"]:
        return False
    return True


def extract_text(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] No se pudo leer {filepath}: {e}")
        return ""


def chunk_text(text: str, file_path: str, cfg: dict) -> list[dict]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg["chunk_size"],
        chunk_overlap=cfg["chunk_overlap"],
        length_function=len,
    )
    chunks = splitter.split_text(text)
    records = []
    search_offset = 0
    for chunk_id, chunk in enumerate(chunks):
        start_offset = text.find(chunk, search_offset)
        if start_offset < 0:
            start_offset = text.find(chunk)
        end_offset = start_offset + len(chunk) if start_offset >= 0 else -1
        record = {"file": file_path, "chunk_id": chunk_id, "content": chunk, "tokens": len(chunk)}
        if start_offset >= 0:
            record["start_line"] = text.count("\n", 0, start_offset) + 1
            record["end_line"] = text.count("\n", 0, end_offset) + 1
            search_offset = start_offset + 1
        records.append(record)
    return records


def collect_valid_files(root_dir: str, cfg: dict) -> list[str]:
    """Recorre el repo y devuelve la lista de archivos válidos."""
    valid_files = []
    for subdir, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in cfg["exclude_dirs"]]
        for filename in files:
            path = os.path.join(subdir, filename)
            if is_valid_file(path, cfg):
                valid_files.append(path)
    return valid_files


def dry_run(root_dir: str, cfg: dict) -> None:
    """Lista los archivos que se procesarían sin ejecutar nada."""
    files = collect_valid_files(root_dir, cfg)

    if not files:
        print("⚠️  No se encontraron archivos válidos para procesar.")
        return

    # Agrupar por extensión
    by_ext: dict[str, list[str]] = {}
    for f in files:
        ext = os.path.splitext(f)[1] or "(sin extensión)"
        by_ext.setdefault(ext, []).append(f)

    print(f"📋 Dry-run: {len(files)} archivos serían procesados\n")
    for ext in sorted(by_ext):
        group = by_ext[ext]
        print(f"  {ext}  ({len(group)} archivos)")
        for path in sorted(group):
            print(f"    • {os.path.relpath(path, root_dir)}")
        print()

    print(f"Total: {len(files)} archivos")


def process_repository(root_dir: str, cfg: dict) -> list[dict]:
    dataset = []
    for path in collect_valid_files(root_dir, cfg):
        content = extract_text(path)
        if not content.strip():
            continue
        dataset.extend(chunk_text(content, path, cfg))
        print(f"[OK] Procesado {path}")
    return dataset


def next_chunk_filename(branch_dir: str) -> str:
    """Devuelve el siguiente nombre secuencial de archivo dentro de la carpeta de la rama."""
    existing = [f for f in os.listdir(branch_dir) if f.endswith("_chunks.jsonl")]
    if not existing:
        return "00001_chunks.jsonl"
    nums = []
    for f in existing:
        try:
            nums.append(int(f.split("_")[0]))
        except ValueError:
            continue
    next_num = (max(nums) + 1) if nums else 1
    return f"{next_num:05d}_chunks.jsonl"


def save_to_jsonl(data: list[dict], output_file: str) -> None:
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for entry in data:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"\n✅ Exportado {len(data)} chunks a {output_file}")


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Segmenta el código fuente en chunks por rama."
    )
    parser.add_argument("branch", nargs="?",
                        help="Rama Git a procesar. Debe ser la rama activa: el indexador no cambia "
                             "de rama. Omitila junto con --current-tree.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Ruta al repositorio a procesar (default: directorio actual).",
    )
    parser.add_argument(
        "--output",
        default="./chunks",
        help="Directorio donde se guardarán los JSONL (default: ./chunks).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista los archivos que se procesarían sin ejecutar nada.",
    )
    parser.add_argument(
        "--current-tree",
        action="store_true",
        help="Indexa la rama activa tal como está en disco, sin comparar con un nombre declarado.",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo)
    output_dir = os.path.abspath(args.output)

    cfg = load_config(repo_dir)

    branch = resolve_branch(repo_dir, args.branch, args.current_tree)

    if args.dry_run:
        print(f"🔎 Dry-run para repo '{repo_dir}' (rama '{branch}')\n")
        dry_run(repo_dir, cfg)
        return

    safe_branch = branch.replace("/", "-").replace("\\", "-")
    branch_dir = os.path.join(output_dir, safe_branch)
    os.makedirs(branch_dir, exist_ok=True)

    next_file = next_chunk_filename(branch_dir)
    output_file = os.path.join(branch_dir, next_file)

    print("🚀 Iniciando limpieza y segmentación del código...")
    data = process_repository(repo_dir, cfg)
    save_to_jsonl(data, output_file)


if __name__ == "__main__":
    main()
