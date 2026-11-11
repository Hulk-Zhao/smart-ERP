"""T305 断点续跑演练的子进程 worker（仅测试用）。

由 ``test_resume_checkpoint.py`` 以**独立进程**拉起，用真实 SIGKILL 制造
「执行中进程消失」。两种注入模式：

- ``kill_after_write``：外部写入已生效（写 ``resume_probe.external_state``）
  后挂起；父进程杀进程 → 本地 ``ticket_step`` 停在 ``intent``。
- ``hang_before_write``：进入 ``write`` 但**尚未**产生外部效果就挂起（只写
  marker、不写 attempt/external_state）；父进程让租约过期、由第二个 worker
  接管后续跑，然后杀掉挂起的进程。

``resume_probe.write_attempt`` 只记录**真正完成的外部调用**，用于断言同一
``idem_key`` 不会被写两次；``resume_probe.marker`` 是跨进程的「已到注入点」
信号。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "tests" / "agents"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from apps.agents.lease import LeaseManager  # noqa: E402
from apps.agents.runtime import PlannedStep, TicketRuntime  # noqa: E402
from apps.agents.store import TicketStore  # noqa: E402
from apps.platform.connectors.sdk import (  # noqa: E402
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.gateway import WriteGateway  # noqa: E402
from apps.platform.policy.levels import ActionSpec, VerifySpec  # noqa: E402
from support import SCENE, AlwaysApproved, make_action  # noqa: E402

HANG_SECONDS = 600.0
PROBE_SCHEMA = "resume_probe"


def plan_steps() -> list[PlannedStep]:
    """三步计划；步骤序号就是 execute 内部的 1..3。"""

    return [
        PlannedStep(
            action=make_action(f"probe.s{i}"),
            target_system="probe",
            payload={"supplier": "SUP-1", "amount": f"{i}0.00"},
            idem_key=f"idem-s{i}",
        )
        for i in (1, 2, 3)
    ]


class ProbeConnector:
    """带幂等键语义的外部系统替身：``external_state`` 唯一键 = idem_key。"""

    meta = ConnectorMeta(
        system="probe",
        version="1.0",
        access_level="L1",
        security_level="A",
        supports_obo=False,
        version_fingerprint="probe-1",
    )

    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession],
        *,
        worker: str,
        mode: str = "normal",
        gate_key: str = "probe.s2",
    ) -> None:
        self._sf = sf
        self.worker = worker
        self.mode = mode
        self.gate_key = gate_key
        #: 进程内调用计数，仅用于诊断输出
        self.calls: list[str] = []

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("probe",), actions=())

    async def read(self, entity: str, query: Any) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Any
    ) -> PreviewResult:
        return PreviewResult(
            diff=(),
            before={"external_id": f"PRE-{action.key}"},
            verify_spec=action.verify or VerifySpec("read_by_external_id"),
        )

    async def _marker(self) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                text(
                    f"INSERT INTO {PROBE_SCHEMA}.marker (name) VALUES (:name) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"name": self.worker},
            )

    async def _commit_external(
        self, action: ActionSpec, ticket_id: str, idem_key: str
    ) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                text(
                    f"INSERT INTO {PROBE_SCHEMA}.write_attempt "
                    "(ticket_id, action_key, idem_key, worker) "
                    "VALUES (:ticket_id, :action_key, :idem_key, :worker)"
                ),
                {
                    "ticket_id": ticket_id,
                    "action_key": action.key,
                    "idem_key": idem_key,
                    "worker": self.worker,
                },
            )
            await session.execute(
                text(
                    f"INSERT INTO {PROBE_SCHEMA}.external_state "
                    "(idem_key, ticket_id, external_id) "
                    "VALUES (:idem_key, :ticket_id, :external_id) "
                    "ON CONFLICT (idem_key) DO NOTHING"
                ),
                {
                    "idem_key": idem_key,
                    "ticket_id": ticket_id,
                    "external_id": f"EXT-{idem_key}",
                },
            )

    async def write(
        self,
        action: ActionSpec,
        payload: Any,
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        self.calls.append(f"write:{action.key}:{idem_key}")
        gated = action.key == self.gate_key
        if gated and self.mode == "hang_before_write":
            await self._marker()
            await asyncio.sleep(HANG_SECONDS)
        await self._commit_external(action, ticket_id, idem_key)
        if gated and self.mode == "kill_after_write":
            await self._marker()
            await asyncio.sleep(HANG_SECONDS)
        return WriteResult(
            external_id=f"EXT-{idem_key}",
            response={"ok": True},
            before_ref="PROBE-BEFORE",
            after_ref="PROBE-AFTER",
        )

    async def verify(self, spec: VerifySpec, payload: Any) -> VerifyResult:
        idem = payload.get("idem_key")
        if not idem:
            # 网关写入后的即时校验：刚写完，按本进程写入结果判 match
            return VerifyResult(status="match", observed={"post_write": True})
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT external_id FROM {PROBE_SCHEMA}.external_state "
                        "WHERE idem_key = :idem_key"
                    ),
                    {"idem_key": idem},
                )
            ).first()
        if row is None:
            return VerifyResult(status="absent", observed={"idem_key": idem})
        return VerifyResult(
            status="match", observed={"idem_key": idem, "external_id": row[0]}
        )

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        return CompensateResult(status="unchanged")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=100.0,
            version_fingerprint="probe-1",
        )


def build_probe_runtime(
    sf: async_sessionmaker[AsyncSession],
    connector: ProbeConnector,
    *,
    lease_ttl_seconds: int,
) -> TicketRuntime:
    gateway = WriteGateway(connector, approvals=AlwaysApproved())

    async def verify(planned: PlannedStep) -> VerifyResult:
        return await connector.verify(
            planned.action.verify or VerifySpec("read_by_external_id"),
            planned.verify_payload or planned.payload,
        )

    async def compensate(call: Any, idem_key: str) -> CompensateResult:
        return await connector.compensate(call, idem_key=idem_key)

    return TicketRuntime(
        store=TicketStore(sf),
        leases=LeaseManager(sf),
        gateway=gateway,
        verifier=verify,
        compensator=compensate,
        max_attempts=1,
        retry_backoff_seconds=(0.0,),
        lease_ttl_seconds=lease_ttl_seconds,
    )


async def _main(args: argparse.Namespace) -> int:
    engine = create_async_engine(args.url, poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    connector = ProbeConnector(
        sf, worker=args.owner, mode=args.mode, gate_key=args.gate
    )
    runtime = build_probe_runtime(sf, connector, lease_ttl_seconds=args.ttl)
    try:
        report = await runtime.execute(
            UUID(args.ticket),
            owner=args.owner,
            steps=plan_steps(),
            scene=SCENE,
        )
        payload = {
            "owner": args.owner,
            "status": report.status.value,
            "recovered": report.recovered,
            "invocations": report.invocations,
            "calls": connector.calls,
        }
    except BaseException as exc:  # noqa: BLE001
        payload = {
            "owner": args.owner,
            "error": f"{type(exc).__name__}: {exc}",
            "calls": connector.calls,
        }
    finally:
        await engine.dispose()
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--ttl", type=int, default=30)
    parser.add_argument(
        "--mode", choices=("normal", "kill_after_write", "hang_before_write"),
        default="normal",
    )
    parser.add_argument("--gate", default="probe.s2")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
