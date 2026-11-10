"""build-images.sh 的脏树门禁（T801 补强）：未提交改动必须中止，--allow-dirty 才放行且 tag 降级。

只做静态/行为验证，不构建任何镜像：全部用 --dry-run，门禁在 docker 之前生效。
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build-images.sh"
TAG = "0" * 39 + "1"  # 合法 40 位小写 sha，内容不重要


def _usable_bash():
    """返回可用的 bash（Windows 上 WSL 的 bash 看不到 Windows 路径，不算）。"""
    candidates = []
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            candidates.append(found)
    else:
        found = shutil.which("bash")
        if found:
            candidates.append(found)
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


BASH = _usable_bash()


def _make_repo(tmp: Path) -> None:
    """造一个满足脚本前置条件的最小仓库（--dry-run 不需要 docker）。"""
    (tmp / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, tmp / "scripts" / "build-images.sh")
    (tmp / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp / "apps").mkdir()
    (tmp / "web").mkdir()
    (tmp / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    docker_dir = tmp / "projects" / "smart-ERP" / "docker"
    docker_dir.mkdir(parents=True)
    for name in ("api", "connector-gw", "docparse", "web"):
        (docker_dir / f"{name}.Dockerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
    git = ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid"]
    subprocess.run(git + ["init", "-q"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(git + ["add", "."], cwd=tmp, check=True, capture_output=True)
    subprocess.run(
        git + ["commit", "-qm", "init"], cwd=tmp, check=True, capture_output=True
    )


def _force_rmtree(path: Path) -> None:
    """Windows 上 git 对象文件是只读的，直接 rmtree 会 PermissionError。"""

    def _on_error(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_on_error)


@unittest.skipUnless(BASH, "本机没有可用的 bash（WSL 壳不算），无法验证门禁")
@unittest.skipUnless(shutil.which("git"), "本机没有 git，无法造脏树")
class DirtyTreeGateTest(unittest.TestCase):
    def setUp(self):
        # Windows 上 .git/objects 可能是只读属性，清理时报 PermissionError；忽略即可
        self._tmp = tempfile.TemporaryDirectory(
            prefix="s8-dirty-gate-", ignore_cleanup_errors=True
        )
        self.tmp = Path(self._tmp.name)
        _make_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args, dist: str | None = None):
        env = dict(os.environ)
        env["ERP_IMAGE_TAG"] = TAG
        env["ERP_DIST_DIR"] = dist or str(self.tmp / "dist")
        return subprocess.run(
            [BASH, "scripts/build-images.sh", "--dry-run", *args],
            check=False,
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_clean_tree_passes(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("工作树洁净", done.stdout)

    def test_dirty_tree_aborts_without_allow_dirty(self):
        (self.tmp / "leftover.txt").write_text("uncommitted\n", encoding="utf-8")
        done = self._run()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("工作树不干净，拒绝构建", done.stderr)
        self.assertIn("leftover.txt", done.stderr)
        # 拒绝时不得打印任何 docker build 计划命令
        self.assertNotIn("+ docker build", done.stdout)
        self.assertNotIn("+ docker save", done.stdout)

    def test_allow_dirty_warns_and_downgrades_tag(self):
        (self.tmp / "leftover.txt").write_text("uncommitted\n", encoding="utf-8")
        done = self._run("--allow-dirty")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        combined = done.stdout + done.stderr
        self.assertIn("工作树不干净", combined)
        self.assertIn("按 --allow-dirty 继续", combined)
        self.assertIn("WARN", combined)
        # tag 必须降级为 <sha>-dirty：deploy-images.sh 会拒绝这种 tag 下发
        self.assertIn(f"{TAG}-dirty", combined)

    def test_non_git_directory_requires_allow_dirty(self):
        _force_rmtree(self.tmp / ".git")
        done = self._run()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("不是 git 仓库", done.stderr)

        done = self._run("--allow-dirty")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"{TAG}-dirty", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
