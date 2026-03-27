# ============================================================================
# Chunking Pipeline — Makefile parametrizable
# ============================================================================
#
# Uso:
#   make chunk REPO=~/projects/agenda2-app
#   make chunk REPO=~/projects/agenda2-app BRANCH=develop
#   make chunk REPO=~/projects/otro-repo BRANCH=feature/x
#
# Pasos individuales:
#   make get    REPO=~/projects/agenda2-app
#   make ingest REPO=~/projects/agenda2-app
#
# ============================================================================

# --- Parámetros configurables ------------------------------------------------
REPO       ?=# (requerido) ruta al repositorio
BRANCH     ?= main# rama a procesar
VENV       ?= /home/daswort/projects/chunking/.venv# ruta al virtualenv
CHUNKS_DIR ?= /home/daswort/projects/chunking/chunks# directorio base de chunks

# --- Derivados ---------------------------------------------------------------
REPO_NAME  := $(notdir $(patsubst %/,%,$(REPO)))
OUTPUT_DIR := $(CHUNKS_DIR)/$(REPO_NAME)
ACTIVATE   := source $(VENV)/bin/activate

# --- Validación --------------------------------------------------------------
.PHONY: _check-repo get ingest chunk clean help

_check-repo:
ifndef REPO
	$(error REPO es requerido. Uso: make chunk REPO=~/projects/mi-repo)
endif
ifeq ($(REPO_NAME),)
	$(error No se pudo determinar el nombre del repo a partir de REPO=$(REPO))
endif

# --- Targets -----------------------------------------------------------------

## Paso 1: generar chunks del repositorio
get: _check-repo
	@echo "══════════════════════════════════════════════════════════════"
	@echo "  chunking-get  │  repo: $(REPO)  │  branch: $(BRANCH)"
	@echo "══════════════════════════════════════════════════════════════"
	@bash -c '$(ACTIVATE) && chunking-get "$(BRANCH)" --repo "$(REPO)" --output "$(OUTPUT_DIR)"'

## Paso 2: ingestar chunks en ChromaDB
ingest: _check-repo
	@echo "══════════════════════════════════════════════════════════════"
	@echo "  chunking-ingest  │  repo: $(REPO)  │  branch: $(BRANCH)"
	@echo "══════════════════════════════════════════════════════════════"
	@bash -c '$(ACTIVATE) && chunking-ingest "$(BRANCH)" --repo "$(REPO)" --chunks-dir "$(OUTPUT_DIR)"'

## Pipeline completo: get + ingest
chunk: get ingest
	@echo ""
	@echo "✅  Pipeline completado para $(REPO_NAME) (branch: $(BRANCH))"

## Limpiar chunks generados para un repo
clean: _check-repo
	@echo "Eliminando $(OUTPUT_DIR)..."
	rm -rf "$(OUTPUT_DIR)"
	@echo "✅  Limpio."

## Mostrar ayuda
help:
	@echo ""
	@echo "Uso:"
	@echo "  make chunk  REPO=<ruta>  [BRANCH=<rama>]   Pipeline completo (get + ingest)"
	@echo "  make get    REPO=<ruta>  [BRANCH=<rama>]   Solo generar chunks"
	@echo "  make ingest REPO=<ruta>  [BRANCH=<rama>]   Solo ingestar en ChromaDB"
	@echo "  make clean  REPO=<ruta>                    Eliminar chunks del repo"
	@echo "  make help                                  Mostrar esta ayuda"
	@echo ""
	@echo "Parámetros:"
	@echo "  REPO       (requerido) Ruta al repositorio"
	@echo "  BRANCH     Rama a procesar (default: main)"
	@echo "  VENV       Ruta al virtualenv (default: $(VENV))"
	@echo "  CHUNKS_DIR Directorio base de chunks (default: $(CHUNKS_DIR))"
	@echo ""
	@echo "Ejemplos:"
	@echo "  make chunk REPO=~/projects/agenda2-app"
	@echo "  make chunk REPO=~/projects/agenda2-app BRANCH=develop"
	@echo "  make clean REPO=~/projects/agenda2-app"
	@echo ""
