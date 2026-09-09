import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chunking import mcp_server
from chunking.ingest_delta import build_metadata, chunk_state_hash, detect_changes


class McpServerTests(unittest.TestCase):
    def test_limit_clamps_invalid_and_excessive_values(self) -> None:
        self.assertEqual(mcp_server._limit(0), 1)
        self.assertEqual(mcp_server._limit(99), mcp_server.MAX_RESULTS)
        self.assertEqual(
            mcp_server._limit(99, mcp_server.MAX_DOCUMENTS_PER_RESPONSE),
            mcp_server.MAX_DOCUMENTS_PER_RESPONSE,
        )

    def test_result_items_respects_total_character_limit(self) -> None:
        results = {
            "documents": [["a" * 8_000, "b" * 8_000, "c" * 8_000, "d" * 8_000]],
            "metadatas": [[
                {"file": "one.py"}, {"file": "two.py"},
                {"file": "three.py"}, {"file": "four.py"},
            ]],
            "distances": [[0.1, 0.2, 0.3, 0.4]],
        }
        items = mcp_server._result_items(results, {"git_sha": "abc"})
        self.assertEqual(sum(len(item["snippet"]) for item in items), mcp_server.MAX_TOTAL_CHARS)
        self.assertTrue(all(len(item["snippet"]) <= mcp_server.MAX_SNIPPET_CHARS for item in items))

    def test_result_items_expose_repo_relative_file_and_absolute_abs_path(self) -> None:
        results = {
            "documents": [["print(1)"]],
            "metadatas": [[{"file": "/repo/src/main.py", "start_line": 1, "end_line": 2}]],
            "distances": [[0.1]],
        }
        [item] = mcp_server._result_items(results, {"git_sha": "abc"}, "/repo")
        self.assertEqual(item["file"], "src/main.py")
        self.assertEqual(item["abs_path"], "/repo/src/main.py")
        self.assertFalse(item["file"].startswith("/"))
        self.assertTrue(item["abs_path"].startswith("/"))

    def test_paths_round_trip_and_survive_an_unknown_repo_root(self) -> None:
        self.assertEqual(mcp_server._repo_root({"repo_path": "/repo/"}), "/repo")
        self.assertIsNone(mcp_server._repo_root(None))
        # una ruta que el servidor devolvió puede volver en cualquiera de las dos formas
        self.assertEqual(mcp_server._stored_path("src/main.py", "/repo"), "/repo/src/main.py")
        self.assertEqual(mcp_server._stored_path("/repo/src/main.py", "/repo"), "/repo/src/main.py")
        # sin raíz conocida, o fuera del repo, la ruta no se toca
        self.assertEqual(mcp_server._public_path("/other/x.py", "/repo"), "/other/x.py")
        self.assertEqual(mcp_server._public_path("/repo/x.py", None), "/repo/x.py")
        self.assertIsNone(mcp_server._public_path(None, "/repo"))

    def test_file_pattern_search_rejects_an_empty_pattern(self) -> None:
        manifests = {"repo_main": {"collection": "repo_main", "repo": "repo", "branch": "main",
                                   "repo_path": "/repo", "files": {"/repo/a.py": 1}}}
        with patch.object(mcp_server, "_load_manifests", return_value=manifests):
            response = mcp_server.search_by_file_pattern("repo_main", "   ", 5)
        self.assertEqual(response["error"], "missing_pattern")
        self.assertIn("hint", response)

    def test_exact_search_rejects_an_oversized_regex(self) -> None:
        response = mcp_server.search_exact("repo_main", regex="x" * (mcp_server.MAX_REGEX_CHARS + 1))
        self.assertEqual(response["error"], "regex_too_long")

    def test_file_pattern_search_returns_repo_relative_files(self) -> None:
        manifests = {
            "repo_main": {
                "collection": "repo_main", "repo": "repo", "branch": "main",
                "repo_path": "/repo", "files": {"/repo/sql/a.sql": 3, "/repo/sql/b.sql": 1},
            }
        }
        with patch.object(mcp_server, "_load_manifests", return_value=manifests), patch.object(
            mcp_server, "_freshness", return_value="unknown"
        ):
            response = mcp_server.search_by_file_pattern("repo_main", ".sql", 5)
        self.assertEqual([f["file"] for f in response["files"]], ["sql/a.sql", "sql/b.sql"])

    def test_resolve_repo_requires_branch_for_ambiguous_repo(self) -> None:
        manifests = {
            "repo_main": {"collection": "repo_main", "repo": "repo", "branch": "main"},
            "repo_dev": {"collection": "repo_dev", "repo": "repo", "branch": "dev"},
        }
        with patch.object(mcp_server, "_load_manifests", return_value=manifests):
            self.assertIsNone(mcp_server._resolve_repo("repo"))
            self.assertEqual(mcp_server._resolve_repo("repo", "dev")["collection"], "repo_dev")

    def test_freshness_details_separates_dirty_from_head_changes(self) -> None:
        manifest = {"repo_path": "/repo", "git_sha": "abc", "branch": "main", "dirty": True}
        with patch.object(mcp_server.os.path, "isdir", return_value=True), patch.object(
            mcp_server, "_git_value", side_effect=["abc", "main", ""]
        ):
            details = mcp_server._freshness_details(manifest)
        self.assertEqual(details["freshness"], "stale")
        self.assertEqual(details["reasons"], ["dirty_at_index"])
        self.assertFalse(details["head_changed"])
        self.assertFalse(details["dirty_now"])

    def test_file_pattern_search_uses_manifest_without_chroma_client(self) -> None:
        manifest = {"collection": "repo_main", "files": {"src/app.py": 3, "tests/app.py": 1}}
        with patch.object(mcp_server, "_load_manifests", return_value={"repo_main": manifest}), patch.object(
            mcp_server, "_collection_context", return_value={"collection": "repo_main"}
        ), patch.object(mcp_server, "_get_client", side_effect=AssertionError("unexpected Chroma query")):
            result = mcp_server.search_by_file_pattern("repo_main", ".py", 99)
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(len(result["files"]), 2)

    def test_destructive_collection_tool_is_not_exposed(self) -> None:
        self.assertFalse(hasattr(mcp_server, "delete_collection"))

    def test_sql_chunks_are_classified_as_tsql(self) -> None:
        metadata = build_metadata({"file": "schema/report.sql", "chunk_id": 0, "tokens": 12})
        self.assertEqual(metadata["language"], "tsql")

    def test_missing_state_uses_the_versioned_hash(self) -> None:
        record = {"file": "schema/report.sql", "chunk_id": 0, "content": "SELECT 1", "tokens": 8}
        with tempfile.TemporaryDirectory() as directory:
            chunks_file = Path(directory) / "00001_chunks.jsonl"
            chunks_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
            changed, deleted_ids, state = detect_changes(directory)
        self.assertEqual(changed, [record])
        self.assertEqual(deleted_ids, [])
        self.assertEqual(state["schema/report.sql:0"], chunk_state_hash(record))

    def test_fastmcp_registers_the_read_only_toolset(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertTrue({"search_repo", "status", "get_collection_summary"} <= names)
        self.assertNotIn("delete_collection", names)


if __name__ == "__main__":
    unittest.main()