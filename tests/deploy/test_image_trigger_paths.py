"""镜像构建触发范围（scripts/image_trigger_paths.py）的回归测试。

守的是「改了东西但流水线没跑」这一类静默故障：仓库新增顶层目录时必须报红，
而不是被悄悄跳过。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "image_trigger_paths.py"

_spec = importlib.util.spec_from_file_location("image_trigger_paths", SCRIPT)
assert _spec and _spec.loader
trigger_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trigger_paths)


class ImageTriggerPathsTest(unittest.TestCase):
    def test_every_top_level_entry_is_covered_or_explicitly_ignored(self):
        self.assertEqual(
            trigger_paths.uncovered_entries(),
            [],
            "仓库根出现了既不在镜像触发列表、也没写豁免理由的顶层条目；"
            "请改 scripts/image_trigger_paths.py（TRIGGER_PREFIXES 或 IRRELEVANT）",
        )

    def test_new_top_level_dir_makes_check_fail(self):
        """模拟新增顶层目录：check 必须非 0，且错误里点名该目录。"""
        original = trigger_paths.top_level_entries
        trigger_paths.top_level_entries = lambda: original() + ["brand_new_pkg"]
        try:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = trigger_paths.check()
        finally:
            trigger_paths.top_level_entries = original
        self.assertEqual(code, 1)
        self.assertIn("brand_new_pkg", stderr.getvalue())

    def test_code_dirs_trigger_image_build(self):
        for path in (
            "domain/docparse/worker.py",
            "core/runtime/scope.py",
            "db/migrations/9999_demo.sql",
            "apps/api/db.py",
            "contracts/config.schema.json",
            "deploy/smart-erp/docker-compose.yml",
            "observability/exporter.py",
            "scripts/build-images.sh",
            "tests/deploy/test_compose_deploy.py",
            ".github/workflows/smart-erp.yml",
            "pyproject.toml",
            "alembic.ini",
        ):
            with self.subTest(path=path):
                self.assertTrue(trigger_paths.is_trigger(path), path)

    def test_docs_and_root_meta_do_not_trigger_image_build(self):
        for path in (
            "docs/product/tech/RUNBOOK.md",
            "README.md",
            ".gitignore",
        ):
            with self.subTest(path=path):
                self.assertFalse(trigger_paths.is_trigger(path), path)

    def test_match_reads_changed_files_from_stdin(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            import sys

            original_stdin = sys.stdin
            sys.stdin = io.StringIO("docs/x.md\nREADME.md\n")
            try:
                code = trigger_paths.main(["match"])
            finally:
                sys.stdin = original_stdin
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "false")


if __name__ == "__main__":
    unittest.main()
