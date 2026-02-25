"""
Segmenta el código fuente de un repositorio en chunks y los guarda como JSONL.

Uso:
    chunking-get <branch> [--repo .] [--output ./chunks] [--dry-run]
"""

import os
import json
import ast
import argparse
import subprocess

from langchain_text_splitters import RecursiveCharacterTextSplitter

from chunking.config import load_config


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
        if filepath.endswith(".py"):
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
            try:
                tree = ast.parse(source)
                docstrings = []
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        doc = ast.get_docstring(node)
                        if doc:
                            docstrings.append(doc)
                return "\n".join(docstrings) + "\n" + source
            except Exception as e:
                print(f"[WARN] Error AST en {filepath}: {e}")
                return source
        else:
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
    return [
        {"file": file_path, "chunk_id": i, "content": chunk, "tokens": len(chunk)}
        for i, chunk in enumerate(chunks)
    ]


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


def switch_branch(branch_name: str, repo_dir: str) -> None:
    """Cambia a la rama indicada en Git."""
    try:
        subprocess.run(["git", "checkout", branch_name], check=True, cwd=repo_dir)
        print(f"🌀 Cambiado a la rama '{branch_name}'")
        subprocess.run(["git", "pull", "origin", branch_name], check=True, cwd=repo_dir)
        print(f"🌀 Bajando los últimos cambios de '{branch_name}'")
    except subprocess.CalledProcessError:
        print(f"[ERROR] No se pudo cambiar a la rama '{branch_name}'. Verifica que exista.")
        exit(1)


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Segmenta el código fuente en chunks por rama."
    )
    parser.add_argument("branch", help="Nombre de la rama Git a procesar.")
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
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo)
    output_dir = os.path.abspath(args.output)

    cfg = load_config(repo_dir)

    if args.dry_run:
        print(f"🔎 Dry-run para repo '{repo_dir}' (rama '{args.branch}')\n")
        dry_run(repo_dir, cfg)
        return

    switch_branch(args.branch, repo_dir)

    safe_branch = args.branch.replace("/", "-").replace("\\", "-")
    branch_dir = os.path.join(output_dir, safe_branch)
    os.makedirs(branch_dir, exist_ok=True)

    next_file = next_chunk_filename(branch_dir)
    output_file = os.path.join(branch_dir, next_file)

    print("🚀 Iniciando limpieza y segmentación del código...")
    data = process_repository(repo_dir, cfg)
    save_to_jsonl(data, output_file)


if __name__ == "__main__":
    main()
