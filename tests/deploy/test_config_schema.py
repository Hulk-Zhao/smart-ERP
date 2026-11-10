#!/usr/bin/env python3
"""T803：config schema / .env.example / 三套环境矩阵的程序化一致性测试。

正向：真实仓库跑 `--all` 必须通过。
负向：每一类漂移都必须让校验器红，防止 schema 写成「看着像能校验」的摆设。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_config_schema as vcs  # noqa: E402

SCHEMA = REPO_ROOT / "contracts" / "config.schema.json"
COMPOSE = REPO_ROOT / "deploy" / "smart-erp" / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "smart-erp" / ".env.example"
MATRIX = REPO_ROOT / "deploy" / "smart-erp" / "env"


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = vcs.main(argv)
    return code, out.getvalue(), err.getvalue()


class ConfigSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s8-schema-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_schema(self) -> Path:
        target = self.tmp / "config.schema.json"
        target.write_text(SCHEMA.read_text(encoding="utf-8"), encoding="utf-8")
        return target

    def write_env(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_all_passes_on_repo(self):
        code, out, err = run(["--all"])
        self.assertEqual(code, 0, err)
        self.assertIn("PASS", out)
        self.assertIn("62 个键", out)

    def test_observability_keys_are_registered(self):
        """代码读点必须在 .env.example 有对应项（配置面不许说谎）。"""
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for key in ("ERP_TRACE_ENABLED", "ERP_TRACE_FILE", "ERP_METRICS_ENABLED", "ERP_METRICS_PATH"):
            self.assertIn(f"\n{key}=", text, f"{key} 未在 .env.example 登记")

    def test_code_key_missing_from_env_example_fails(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        props = vcs.validate_schema(schema, vcs.Report())
        tmp_env = self.tmp / ".env.example"
        # 故意删掉 ERP_TRACE_ENABLED，模拟「代码在读、模板里查不到」
        body = ENV_EXAMPLE.read_text(encoding="utf-8").replace("ERP_TRACE_ENABLED=", "# removed\n")
        tmp_env.write_text(body, encoding="utf-8")
        rep = vcs.Report()
        vcs.validate_code(props, rep, tmp_env)
        self.assertEqual(rep.flush(), 1)
        self.assertTrue(
            any("未在 .env.example 登记的键 ERP_TRACE_ENABLED" in msg for msg in rep.errors),
            rep.errors,
        )

    def test_every_compose_key_is_in_env_example(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        missing = [
            key
            for key, spec in schema["properties"].items()
            if spec.get("scope") == "compose" and f"\n{key}=" not in text
        ]
        self.assertEqual(missing, [], f"compose 键未在 .env.example 登记：{missing}")

    def test_matrix_covers_required_keys_with_real_values(self):
        required = json.loads(SCHEMA.read_text(encoding="utf-8"))["required"]
        for name in ("local.env", "demo.env", "prod.env"):
            values = {}
            for line in (MATRIX / name).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    values[key] = value
            for key in required:
                self.assertIn(key, values, f"{name} 缺必填键 {key}")
                self.assertTrue(values[key].strip(), f"{name} 的 {key} 为空")

    def test_unknown_env_key_fails(self):
        env = self.write_env("bad.env", "ERP_NOT_REGISTERED=1\n")
        code, _, err = run(["--schema", str(SCHEMA), "--compose", str(COMPOSE), "--env", str(env)])
        self.assertEqual(code, 1)
        self.assertIn("未登记的键", err)

    def test_integer_type_error_fails(self):
        env = self.write_env("bad-type.env", "ERP_DB_POOL_SIZE=abc\n")
        code, _, err = run(["--schema", str(SCHEMA), "--compose", str(COMPOSE), "--env", str(env)])
        self.assertEqual(code, 1)
        self.assertIn("需要整数", err)

    def test_blank_required_in_matrix_fails_but_template_allows_blank(self):
        body = "ERP_DATABASE_URL=\nERP_MIGRATION_DATABASE_URL=x\nERP_IMAGE_TAG=dev\n"
        real = self.write_env("real.env", body)
        code, _, err = run(["--schema", str(SCHEMA), "--compose", str(COMPOSE), "--env", str(real)])
        self.assertEqual(code, 1)
        self.assertIn("必填键 ERP_DATABASE_URL 为空", err)

        template = self.write_env(".env.example", body)
        code, _, err = run(["--schema", str(SCHEMA), "--compose", str(COMPOSE), "--env", str(template)])
        self.assertEqual(code, 0, err)

    def test_schema_default_disagrees_with_compose_fails(self):
        path = self.copy_schema()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["properties"]["ERP_API_PORT"]["default"] = 9999
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, _, err = run(["--schema", str(path), "--compose", str(COMPOSE)])
        self.assertEqual(code, 1)
        self.assertIn("ERP_API_PORT", err)
        self.assertIn("不一致", err)

    def test_unregistered_compose_key_fails(self):
        path = self.copy_schema()
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["properties"]["ERP_WEB_PORT"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, _, err = run(["--schema", str(path), "--compose", str(COMPOSE)])
        self.assertEqual(code, 1)
        self.assertIn("ERP_WEB_PORT", err)

    def test_per_service_defaults_must_be_declared(self):
        path = self.copy_schema()
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["properties"]["ERP_DB_POOL_SIZE"]["defaultsByService"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, _, err = run(["--schema", str(path), "--compose", str(COMPOSE)])
        self.assertEqual(code, 1)
        self.assertIn("多个默认值", err)
        self.assertIn("defaultsByService", err)

    def test_compose_required_must_match_schema(self):
        path = self.copy_schema()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["properties"]["ERP_API_PORT"]["required"] = True
        data["required"].append("ERP_API_PORT")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, _, err = run(["--schema", str(path), "--compose", str(COMPOSE)])
        self.assertEqual(code, 1)
        self.assertIn("ERP_API_PORT", err)
        self.assertIn(":?", err)


if __name__ == "__main__":
    unittest.main()
