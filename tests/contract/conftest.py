"""G0 门禁的「静默跳过 = 失败」守卫。

门禁必须能区分「用例通过」与「用例被跳过」：pytest 默认把「全部 skip」的会话判成
exit 0，那门禁就只是装饰。这里在会话结束时把任何 skip 或「0 收集」改成失败，并把
每条 skip 的原因打印出来。守卫放在 conftest 里，跟随 ``pytest tests/contract``
自动生效，不依赖 CI 里的外部 grep 约定。
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter is not None else []
    if skipped:
        print(f"\n[G0 门禁] 检测到 {len(skipped)} 条被跳过的用例，门禁判红：")
        for report in skipped:
            print(f"  SKIP {report.nodeid}\n       原因: {report.longrepr}")
        session.exitstatus = 1
    if session.testscollected == 0:
        print("\n[G0 门禁] 未收集到任何用例（tests/contract 被清空或路径写错），门禁判红")
        session.exitstatus = 1
