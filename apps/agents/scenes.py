"""AG-1~AG-8 场景声明（PRD §5）。

``max_level`` 是代码常量，不读配置：配置只能通过 ``effective_level`` 往下压，
不能抬高。AG-5 不写任何系统，固定 W0。
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.platform.policy.levels import SceneSpec, WriteLevel


@dataclass(frozen=True, slots=True)
class AgentScene:
    scene: SceneSpec
    name: str
    # 可写入的最早状态（PRD 「可写入的最早状态」列），运行时只做提示，不据此放行
    earliest_write_state: str
    money_fields: tuple[str, ...] = ()


_RAW: tuple[AgentScene, ...] = (
    AgentScene(SceneSpec("AG-1", WriteLevel.W1), "对账 Agent", "待审批", ("diff_amount", "amount")),
    AgentScene(SceneSpec("AG-2", WriteLevel.W1), "单据录入 Agent", "待审批", ("amount",)),
    AgentScene(SceneSpec("AG-3", WriteLevel.W1), "采购比价 Agent", "待审批", ("price", "amount")),
    # 预留/调拨回写为 W2（阈值内免审），所以上限取 W2；草稿仍是 W1
    AgentScene(SceneSpec("AG-4", WriteLevel.W2), "库存与齐套 Agent", "待审批", ("amount",)),
    AgentScene(SceneSpec("AG-5", WriteLevel.W0), "生产排程 Agent", "不写入", ()),
    AgentScene(SceneSpec("AG-6", WriteLevel.W1), "异常处置 Agent", "待审批", ("cost_delta",)),
    AgentScene(SceneSpec("AG-7", WriteLevel.W1), "报价 Agent", "待审批", ("price", "amount")),
    AgentScene(SceneSpec("AG-8", WriteLevel.W1), "月结辅助 Agent", "待审批", ("amount",)),
)

AGENT_SCENES: dict[str, AgentScene] = {item.scene.code: item for item in _RAW}

ALL_SCENE_CODES: tuple[str, ...] = tuple(AGENT_SCENES)


def scene_of(code: str) -> AgentScene:
    try:
        return AGENT_SCENES[code]
    except KeyError as exc:
        raise ValueError(f"未注册的 Agent 场景: {code!r}（允许 {sorted(AGENT_SCENES)}）") from exc


def scene_specs() -> dict[str, SceneSpec]:
    return {code: item.scene for code, item in AGENT_SCENES.items()}
