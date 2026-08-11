from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_repository.py"
SPEC = importlib.util.spec_from_file_location("check_repository", MODULE_PATH)
assert SPEC and SPEC.loader
CHECK_REPOSITORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK_REPOSITORY)


class RepositoryChecksTest(unittest.TestCase):
    def test_accepts_public_repository_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "README.md").write_text(CHECK_REPOSITORY.README_OPENING, encoding="utf-8")
            (root / "LICENSE").write_text("GPL\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_contract([root / "README.md", root / "LICENSE"])
        self.assertEqual(errors, [])

    def test_rejects_private_or_generated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "README.md").write_text(CHECK_REPOSITORY.README_OPENING, encoding="utf-8")
            (root / "LICENSE").write_text("GPL\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_contract(
                    [
                        root / "docs" / "plans" / "private.md",
                        root / "docs" / "prd" / "private.md",
                        root / "graphify-out" / "graph.json",
                    ]
                )
        self.assertIn("forbidden tracked path: docs/plans/private.md", errors)
        self.assertIn("forbidden tracked path: docs/prd/private.md", errors)
        self.assertIn("forbidden tracked path: graphify-out/graph.json", errors)

    def test_allows_public_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "README.md").write_text(CHECK_REPOSITORY.README_OPENING, encoding="utf-8")
            (root / "LICENSE").write_text("GPL\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_contract([root / "docs" / "architecture.md"])
        self.assertEqual(errors, [])

    def test_reports_text_hygiene_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "bad.cpp"
            source.write_text("const char* value = 'bad'; // \u2014\t ", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_text([source])
        self.assertIn("missing final newline: bad.cpp", errors)
        self.assertIn("trailing whitespace: bad.cpp:1", errors)
        self.assertIn("tab found: bad.cpp:1", errors)
        self.assertIn("non-ASCII source decoration: bad.cpp", errors)

    def test_allows_non_ascii_content_letters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "good.cpp"
            source.write_text("const char* value = 'caf\u00e9 \u30bd';\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_text([source])
        self.assertEqual(errors, [])

    def test_allows_generated_lockfile_line_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lockfile = root / "uv.lock"
            lockfile.write_text(f"archive = {'x' * 140}\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "ROOT", root):
                errors = CHECK_REPOSITORY.check_text([lockfile])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
