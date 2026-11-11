# 权限矩阵证据（客户安全尽调版）

> 口径日期：2026-11-11 ｜ 代码版本：当前工作树 ｜ 范围：`apps/security/`、`apps/audit/`、`apps/platform/policy/`、`apps/api/` 的权限行为
> 本文件为本地留存（`docs/product/` 在 .gitignore 内），不随代码发布，供尽调直接出示。

测试分布：矩阵与硬约束在 `tests/security/test_permission_matrix.py`（夹具 `tests/security/matrix_support.py`），
C1/C2/C3/C5 层内证据在 `tests/security/test_role_gates.py`，C6 配置授权在 `tests/security/test_config_access.py`。
复现：

```bash
uv run --extra dev --python 3.12 python -m pytest tests/security tests/audit -q -p no:warnings
# 218 passed, 7 skipped（跳过项均为「依赖 apps/api/service.py 接线放行」，见 §9）
```

> 状态说明：C1/C2/C5 的**策略层**已落地并有测试；但 `apps/api/service.py` 与路由接线被对账持久化流占用，
> 尚未放行，因此涉及 HTTP 行为的格子（导出、服务账号读/影像）标 **⏸ 待接线** 并从套件中 skip，
> 不用当前行为冒充期望结果。

---

## 1. 一句话结论

- **可出示的证据**：5 类角色 × 7 类操作 × 3 类资源归属 = 105 个格子逐格有测试，另有 14 条硬约束测试；
  拒绝格子一律断言「连接器零预览、零写入」，证明拒绝发生在写闸门之前。
- **硬约束成立**：自审在接口层与 DB 约束双验；跨组织/跨批次（含伪造 org 头）一律拒绝；
  B 级服务账号强制双人、凭据最高 W1、**W1+ 无白名单即拒绝**、白名单外字段拒写；
  W3 在网关、审批、回滚三条路径都无执行分支。W0 动作不进审批链（现行即如此）；
  **W0 免白名单**与**删除类 W0 仅作为补偿动作**随 `feat/s1-demo-write-path` 合入生效，细则见 §4.7。
- **冲突处置进度**：C3（B 级白名单收窄）、C5（业务类别归属）、C6（配置授权闸口）已修复并转绿；
  C1（回滚他人带理由）、C2（读/影像/导出角色闸）策略层已落地，HTTP 接线待放行；
  C7（批次维度）登记为已知限制 + 触发条件，C8（计费视图）后置到计量线。

---

## 2. 维度定义

### 2.1 角色（5）

| 角色 | 测试身份 | 会话 roles | 对应 PRD 角色 |
|---|---|---|---|
| 发起人 | `initiator` | `operator` | 财务对账 / 采购 / 计划车间（业务发起人） |
| 审批人 | `approver` | `approver` | 审批人（超阈值第二签） |
| 审计员 | `auditor` | `auditor` | 审计只读 |
| 平台管理员 | `admin` | `platform_admin` | IT/CIO、平台管理员 |
| 服务账号 | `service_account` | `service_account` | PRD 无对应角色；由 B 级连接器降级推导 |

「服务账号」不是人工登录角色，而是 `TokenBroker.exchange` 在 B 级连接器上的降级结果
（`mode=service_account`）。矩阵中它既以独立会话出现（验证无业务审批能力），
也以 B 级租约形式单独验证（§4.3）。

### 2.2 操作（7）

| 操作 | HTTP 入口 | 代码入口 |
|---|---|---|
| 读工单 | `GET /erp/api/v1/tickets/{id}` | `service.get_ticket` |
| 写工单（执行写计划） | `POST .../tickets/{id}/actions/approve` | `service.approve_ticket` → `AccessGate` → `WriteGateway` |
| 审批 | `POST .../approvals/{id}/approve` | `service.decide_approval` → `ApprovalService.record` |
| 驳回 | `POST .../approvals/{id}/reject` | 同上 |
| 回滚 | `POST .../tickets/{id}/actions/compensate` | `service.compensate_ticket` → `RollbackService` |
| 导出审计 | `GET .../audit/export` | `service.export_audit_csv` |
| 看影像 | `GET .../audit/tickets/{id}/images` | `service.list_audit_images` |

### 2.3 资源归属（3）

| 归属 | 构造方式 | 说明 |
|---|---|---|
| 本组织 | `org_id=ORG-1, period=2026-10` | 与调用方授权范围一致 |
| 跨组织 | 工单 `org_id=ORG-2`，调用方仍请求本组织 scope | 资源级校验 `_require_ticket_in_scope` |
| 跨批次 | 工单 `period=2026-09`（另一对账批次/期间） | `Scope` 无独立批次维度，**用 period 作批次代理** |

> **已知限制（C7）**：`Scope` 只有 `org_id / period / system_ids`，批次（batch）只能由期间代理。
> **触发条件**：客户明确要求「同一期间内按对账批次分别授权/隔离」时，才变更 `Scope` 契约
> （新增 `batch_ids` 并同步 RLS 与令牌授权范围），走契约变更门（OQ-16）。POC 阶段 period 代理可接受。

> 注意：跨组织/跨批次列同时覆盖两种情况——① 调用方把 scope 改成他人组织（请求级越权）；
> ② 调用方请求自己的 scope 去命中他人资源（资源级越权）。矩阵主表用 ②，
> ① 与伪造 `X-Org-Id` 头由 §4.2 专测。

---

## 3. 主矩阵（角色 × 操作 × 资源归属）

图例：✅ 允许 ｜ ⛔ 拒绝（零外部写入） ｜ ⚠️ 需双人（单签放行但 `satisfied=false`）
格子内为测试节点后缀，完整节点为
`tests/security/test_permission_matrix.py::test_permission_matrix_cell[<后缀>]`。

### 3.1 发起人（initiator）

| 操作 | 本组织 | 跨组织 | 跨批次 |
|---|---|---|---|
| 读工单 | ✅ 允许<br>`initiator__read__same_org` | ⛔ 拒绝 403<br>`initiator__read__cross_org` | ⛔ 拒绝 403<br>`initiator__read__cross_period` |
| 写工单 | ✅ 允许<br>`initiator__write__same_org` | ⛔ 拒绝 403<br>`initiator__write__cross_org` | ⛔ 拒绝 403<br>`initiator__write__cross_period` |
| 审批 | ⛔ 拒绝 409（自审）<br>`initiator__approve__same_org` | ⛔ 403<br>`initiator__approve__cross_org` | ⛔ 403<br>`initiator__approve__cross_period` |
| 驳回 | ⛔ 拒绝 409（自审）<br>`initiator__reject__same_org` | ⛔ 403<br>`initiator__reject__cross_org` | ⛔ 403<br>`initiator__reject__cross_period` |
| 回滚 | ✅ 允许（自己发起）<br>`initiator__rollback__same_org` | ⛔ 拒绝 403<br>`initiator__rollback__cross_org` | ⛔ 拒绝 403<br>`initiator__rollback__cross_period` |
| 导出审计 | ⏸ 待接线（目标：⛔ 拒绝 403）<br>层内 `test_c2_export_denied_for_business_roles[operator]`；HTTP `initiator__export__same_org` | ⛔ 403<br>`initiator__export__cross_org` | ⛔ 403<br>`initiator__export__cross_period` |
| 看影像 | ✅ 允许<br>`initiator__images__same_org` | ⛔ 403<br>`initiator__images__cross_org` | ⛔ 403<br>`initiator__images__cross_period` |

### 3.2 审批人（approver）

| 操作 | 本组织 | 跨组织 | 跨批次 |
|---|---|---|---|
| 读工单 | ✅ 允许<br>`approver__read__same_org` | ⛔ 403<br>`approver__read__cross_org` | ⛔ 403<br>`approver__read__cross_period` |
| 写工单 | ⛔ 拒绝 403（非发起人）<br>`approver__write__same_org` | ⛔ 403<br>`approver__write__cross_org` | ⛔ 403<br>`approver__write__cross_period` |
| 审批 | ⚠️ 需双人（单签 200，`satisfied=false`）<br>`approver__approve__same_org` | ⛔ 403<br>`approver__approve__cross_org` | ⛔ 403<br>`approver__approve__cross_period` |
| 驳回 | ✅ 允许<br>`approver__reject__same_org` | ⛔ 403<br>`approver__reject__cross_org` | ⛔ 403<br>`approver__reject__cross_period` |
| 回滚 | ⛔ 拒绝 403<br>`approver__rollback__same_org` | ⛔ 403<br>`approver__rollback__cross_org` | ⛔ 403<br>`approver__rollback__cross_period` |
| 导出审计 | ⏸ 待接线（目标：⛔ 拒绝 403）<br>层内 `test_c2_export_denied_for_business_roles[approver]`；HTTP `approver__export__same_org` | ⛔ 403<br>`approver__export__cross_org` | ⛔ 403<br>`approver__export__cross_period` |
| 看影像 | ✅ 允许<br>`approver__images__same_org` | ⛔ 403<br>`approver__images__cross_org` | ⛔ 403<br>`approver__images__cross_period` |

### 3.3 审计员（auditor）

| 操作 | 本组织 | 跨组织 | 跨批次 |
|---|---|---|---|
| 读工单 | ✅ 允许<br>`auditor__read__same_org` | ⛔ 403<br>`auditor__read__cross_org` | ⛔ 403<br>`auditor__read__cross_period` |
| 写工单 | ⛔ 拒绝 403<br>`auditor__write__same_org` | ⛔ 403<br>`auditor__write__cross_org` | ⛔ 403<br>`auditor__write__cross_period` |
| 审批 | ⛔ 拒绝 409（角色不符）<br>`auditor__approve__same_org` | ⛔ 403<br>`auditor__approve__cross_org` | ⛔ 403<br>`auditor__approve__cross_period` |
| 驳回 | ⛔ 拒绝 409（角色不符）<br>`auditor__reject__same_org` | ⛔ 403<br>`auditor__reject__cross_org` | ⛔ 403<br>`auditor__reject__cross_period` |
| 回滚 | ⛔ 拒绝 403<br>`auditor__rollback__same_org` | ⛔ 403<br>`auditor__rollback__cross_org` | ⛔ 403<br>`auditor__rollback__cross_period` |
| 导出审计 | ✅ 允许<br>`auditor__export__same_org` | ⛔ 403<br>`auditor__export__cross_org` | ⛔ 403<br>`auditor__export__cross_period` |
| 看影像 | ✅ 允许<br>`auditor__images__same_org` | ⛔ 403<br>`auditor__images__cross_org` | ⛔ 403<br>`auditor__images__cross_period` |

### 3.4 平台管理员（admin）

| 操作 | 本组织 | 跨组织 | 跨批次 |
|---|---|---|---|
| 读工单 | ✅ 允许<br>`admin__read__same_org` | ⛔ 403<br>`admin__read__cross_org` | ⛔ 403<br>`admin__read__cross_period` |
| 写工单 | ⛔ 拒绝 403（非发起人）<br>`admin__write__same_org` | ⛔ 403<br>`admin__write__cross_org` | ⛔ 403<br>`admin__write__cross_period` |
| 审批 | ⛔ 拒绝 409（职责分离）<br>`admin__approve__same_org` | ⛔ 403<br>`admin__approve__cross_org` | ⛔ 403<br>`admin__approve__cross_period` |
| 驳回 | ⛔ 拒绝 409（职责分离）<br>`admin__reject__same_org` | ⛔ 403<br>`admin__reject__cross_org` | ⛔ 403<br>`admin__reject__cross_period` |
| 回滚 | ⛔ 拒绝 403<br>`admin__rollback__same_org` | ⛔ 403<br>`admin__rollback__cross_org` | ⛔ 403<br>`admin__rollback__cross_period` |
| 导出审计 | ✅ 允许<br>`admin__export__same_org` | ⛔ 403<br>`admin__export__cross_org` | ⛔ 403<br>`admin__export__cross_period` |
| 看影像 | ✅ 允许<br>`admin__images__same_org` | ⛔ 403<br>`admin__images__cross_org` | ⛔ 403<br>`admin__images__cross_period` |

### 3.5 服务账号（service_account）

| 操作 | 本组织 | 跨组织 | 跨批次 |
|---|---|---|---|
| 读工单 | ⏸ 待接线（目标：⛔ 拒绝 403）<br>层内 `test_c2_service_account_cannot_read_tickets`；HTTP `service_account__read__same_org` | ⛔ 403<br>`service_account__read__cross_org` | ⛔ 403<br>`service_account__read__cross_period` |
| 写工单 | ⛔ 拒绝 403（非发起人；B 级路径见 §4.3）<br>`service_account__write__same_org` | ⛔ 403<br>`service_account__write__cross_org` | ⛔ 403<br>`service_account__write__cross_period` |
| 审批 | ⛔ 拒绝 409<br>`service_account__approve__same_org` | ⛔ 403<br>`service_account__approve__cross_org` | ⛔ 403<br>`service_account__approve__cross_period` |
| 驳回 | ⛔ 拒绝 409<br>`service_account__reject__same_org` | ⛔ 403<br>`service_account__reject__cross_org` | ⛔ 403<br>`service_account__reject__cross_period` |
| 回滚 | ⛔ 拒绝 403<br>`service_account__rollback__same_org` | ⛔ 403<br>`service_account__rollback__cross_org` | ⛔ 403<br>`service_account__rollback__cross_period` |
| 导出审计 | ⏸ 待接线（目标：⛔ 拒绝 403）<br>层内 `test_c2_export_denied_for_business_roles[service_account]`；HTTP `service_account__export__same_org` | ⛔ 403<br>`service_account__export__cross_org` | ⛔ 403<br>`service_account__export__cross_period` |
| 看影像 | ⏸ 待接线（目标：⛔ 拒绝 403）<br>层内同读闸 `test_c2_service_account_cannot_read_tickets`；HTTP `service_account__images__same_org` | ⛔ 403<br>`service_account__images__cross_org` | ⛔ 403<br>`service_account__images__cross_period` |

---

## 4. 硬约束证据

### 4.1 审批人 ≠ 发起人（接口层 + DB 层双验）

| 层次 | 证据 | 测试 |
|---|---|---|
| 接口层（服务入口） | `ApprovalService.record` 抛 `SelfApprovalRejected`；HTTP 409 `E-POLICY-APPROVAL` | `test_b1_self_approval_rejected_at_service_layer`、矩阵 `initiator__approve__same_org` |
| DB 层（兜底） | 生成的 PG DDL 含 `CHECK (approver_id <> initiator_id)` 与 `UNIQUE (ticket_id, approver_id)` | `test_b1_self_approval_db_check_and_unique_constraints` |
| 同一人不得双角色 | `UNIQUE (ticket_id, approver_id)` + `DuplicateApprover` | 同上（DDL 断言）+ `tests/security/test_approvals.py` |

> 边界：DB 层证据是**编译出的 DDL 断言**，未在 PG15 实跑（本机无隔离库）。
> 实跑迁移由 S0/T006 负责，尽调如需可现场执行 `alembic upgrade head` 后做插入验证。

### 4.2 跨组织一律拒绝（含伪造 org 头）

| 场景 | 结果 | 测试 |
|---|---|---|
| 伪造 `X-Org-Id` / `X-Org` / `X-Tenant-Id` 头，scope 保持本组织 | 200，且列表只含本组织工单（头被忽略） | `test_b2_forged_org_headers_cannot_widen_scope` |
| 把 `org_id=ORG-2` 写进查询参数 + 伪造头 | 403 `E-AUTHZ-001`，零写入 | `test_b2_forged_org_param_is_rejected` |
| 用本组织 scope 去命中 `ORG-2` 的工单 | 403 `E-AUTHZ-001` | 矩阵 `*__*__cross_org` 共 35 格 |
| 本组织 scope 命中另一期间（跨批次）的工单 | 403 `E-AUTHZ-001` | `test_b2_cross_period_uses_same_deny_path` + 矩阵 `*__*__cross_period` |

> 实现口径：API 只认请求头 `X-Session-Token`（或 `Authorization: Bearer`），**不读任何 org 头**；
> org 只能来自 query/body 的 `scope`，且必须 ⊆ 会话授权范围。

### 4.3 服务账号（B 级）双人 + 字段白名单

| 约束 | 结果 | 测试 |
|---|---|---|
| **W1+** 无字段白名单 | ⛔ `MissingFieldWhitelist`（fail-closed，不降级放大）；W0 动作免白名单随 S1 合入生效（当前 main 仍要求） | `test_b3_b_level_requires_field_whitelist` |
| 等级上限 | ⛔ **动作等级** W2 → `LevelNotAllowed`；租约 `max_level=W1`。W2 **工单**执行 W1 动作不再被闸3 误杀（S1 合入后；审批仍按工单等级，见 §4.7） | `test_b3_b_level_caps_at_w1`；W2 工单 + W1 动作用例在 `feat/s1-demo-write-path`（未合入 main） |
| W0 免白名单的边界（S1 合入后） | 只放开字段白名单，不放开 scope/租户/组织；跨租户/跨组织仍走同一拒绝路径 | —（无独立用例，记为证据缺口） |
| 强制双人 | 单签 → `ApprovalNotSatisfied`；双签（reviewer + approver）→ 放行 | `test_b3_b_level_forces_dual_review_and_records_degradation` |
| 降级留痕 | `lease.degradation` 非空、`broker.degradations` 有记录 | 同上 |
| **字段白名单对 payload 生效** | ✅ 已修复（C3）：白名单外字段 → `FieldScopeViolation`，且零预览零写入；子集正常写入 | `test_b3_narrowed_fields_reject_out_of_whitelist_payload`、`test_c3_narrowed_fields_reject_out_of_whitelist_payload`、`test_c3_narrowed_fields_allow_subset` |
| **API 路径的 B 级写** | ⏸ 待接线（C4）：API 尚未下传白名单；接线后目标为 200 且预演/写入/校验各一次 | `test_c4_b_level_ticket_completes_intent_to_verify_over_http`（skip）；改前留证 `test_b3_b_level_api_write_fails_closed_without_whitelist_wiring`（skip） |

### 4.4 W3 对任何角色都拒绝

| 路径 | 结果 | 测试 |
|---|---|---|
| 四道闸（5 类角色逐一） | `W3AccessDenied`，零预览零写入 | `test_b4_w3_is_denied_for_every_role` |
| HTTP 执行路径 | 403 `E-POLICY-W3FORBIDDEN` | `test_b4_w3_api_path_is_rejected_without_side_effect` |
| 写闸门（伪造 W3 ActionSpec） | `ForbiddenWriteError`，无执行分支 | `test_b4_write_gateway_has_no_w3_branch` |
| 审批路径 | `W3NotApprovable` | `test_b4_w3_is_not_approvable` |
| 回滚路径（原步骤 W3） | `RollbackForbidden`，整单拒绝 | `tests/audit/test_rollback.py::test_w3_original_refuses_whole_rollback_without_any_write` |

### 4.5 回滚他人工单的角色 + 理由（C1）

| 约束 | 结果 | 测试 |
|---|---|---|
| 自己发起 | 允许，无需理由 | `test_c1_own_ticket_rollback_needs_no_reason` |
| IT/CIO、CFO 回滚他人 | 允许，但理由必填 | `test_c1_other_ticket_rollback_allowed_with_reason`（`platform_admin` / `cfo` 各一） |
| 理由缺失 / 空白串 | 一律拒绝 `RollbackReasonRequired` | `test_c1_other_ticket_rollback_requires_non_blank_reason`（2 角色 × 2 种空值） |
| 其余角色回滚他人 | 拒绝 `OtherInitiatorRollbackDenied` | `test_c1_other_ticket_rollback_denied_for_business_roles` |
| 理由与回滚人进审计 | 回滚前追加 `*:rollback_request` 影像，快照含 `reason/actor_id/actor_roles`；哈希链仍可验 | `test_c1_rollback_reason_and_actor_are_written_to_audit` |
| 无理由的旧调用 | 不额外写影像，旧证据链形态不变 | `test_c1_rollback_without_reason_keeps_audit_shape` |

> HTTP 层：`compensate_ticket` 已接入 `assert_can_rollback` 与 `ActionIn.reason`（S1 分支）；删除类动作、开关与写出口口径见 §4.7。

### 4.6 复核/审批按业务类别归属（C5）

| 约束 | 结果 | 测试 |
|---|---|---|
| 财务类场景 + `finance` 角色 | 允许 | `test_c5_reviewer_with_matching_category_is_allowed` |
| 采购类角色复核财务类场景 | 拒绝 `CrossCategoryReviewRejected` | `test_c5_cross_category_review_is_rejected` |
| 未登记场景 | 拒绝 `UnknownBusinessCategory`（fail-closed） | `test_c5_unknown_scene_has_no_review_authority` |
| 新场景登记类别后 | 允许 | `test_c5_new_scene_can_register_category` |
| 未传 `scene_code` 的旧调用 | 仅校验角色（兼容路径） | 既有 `tests/security/test_approvals.py` 全部通过 |

> 当前唯一登记：`ag2_processing_fee → finance`。新增场景必须显式 `register`，否则复核/审批直接拒绝。

---

### 4.7 W0 动作、删除与补偿路径（2026-11-11 复核口径）

> 口径来源：`feat/s1-demo-write-path @ 455c0ed` 的安全层复核（PR 暂压，未合入 main）。
> 本节写「合入后的规则」；当前 main 上没有 U8 连接器装配，相关动作不可达。

**规则**

1. **W0 动作不进审批链。** 审批角色由工单治理等级 `ticket.write_level` 决定（闸4）；写闸只在 `effective_level >= W1` 时检查审批。W0 动作（只读、对账、审批记录、删除草稿）不触发审批链，控制它的是各入口自身的授权。
2. **删除类 W0 仅允许作为补偿动作。** `u8.po.delete_draft` / `u8.arrival.delete_draft` 声明为 W0（`writable_fields={cCode}`，`verify=read_*_by_code`），只由 `RollbackPlanner` 从已成功正向步骤的反向动作取出；正向 `pending_write` 使用 `delete_*` 由 `ActionSpec.compensation_only` 拒绝（落地状态见文末）。
3. **删除的控制项是回滚授权，不是审批。** 触发补偿必须同时满足：`assert_can_rollback`（**自己发起不限角色**；**他人工单仅 IT/CIO/CFO 且理由非空**）、`ERP_ROLLBACK_SYNC_ENABLED`（**默认 false**）、工单处于**执行中/补偿中**、声明 `fail_code`。删除对象取原成功步骤审计影像的 `object_id`（U8 回传的 cCode），调用方不能指定。
4. **补偿写出口的范围。** **同步补偿**（`service.compensate_ticket` → `RollbackService`）只经 `WriteGateway`，并写 `*:rollback` 审计影像（before=草稿全文、after={cCode, deleted}，`field_diff` 自动计算）。**异步补偿 worker 不经 `WriteGateway`**，直接调连接器 → `transport.delete_draft`，也不写审计影像（既有实现，非本轮引入）。因此「回滚只能经 `WriteGateway`」只对同步入口成立，不能作为全局结论。
5. **删除的留痕边界。** `*:rollback` 影像的 `before_ref`/`after_ref` 是业务键（cCode），不落快照库；被删草稿的字段级前像保存在 `field_diff` 中，完整正文不单独留存。他人回滚会先追加 `*:rollback_request` 影像（reason/actor_id/actor_roles）。

**确实变了（相对上一版矩阵）**

| # | 项 | 旧口径 | 新口径 |
|---|---|---|---|
| 1 | B 级凭据上限的比对对象 | 用**工单治理等级**比 `lease.max_level`，B 级下 W2 工单永远不可执行 | 用**动作等级**比（`AccessRequest.credential_level`，缺省等于 `level`）；审批链仍按工单等级 |
| 2 | B 级字段白名单 | 所有动作都要白名单 | 仅 **W1+** 强制；W0 读/审批不再要求 |
| 3 | u8 回滚 | `create_draft` 的补偿没有已注册反向动作 → 不可执行（manual task） | 真实 W0 DELETE，删本工单自建的草稿 |

**不变**

- W3 在闸、网关、审批、回滚四路都无执行分支：`level` 或 `credential_level` 任一为 W3 即拒。
- 审批链仍按工单治理等级；B 级下工单等级的审批角色集是动作等级审批集的超集。
- 跨租户/跨组织 scope 判据不变；W0 放开白名单只影响字段收窄，不影响 scope。

**最坏情形（只写到这里）**

- B 级连接器上，一次**已按 W2 审批**的工单执行一次 W1 草稿写。
- 同步回滚开关打开时，发起人（或 IT/CIO/CFO + 理由）可删掉该工单自己创建的草稿；删除前不重新校验草稿是否已被他人修改。
- 当前没有 HTTP 建单入口，构造不出「W0 工单 + 删除动作」的无审批删除。

**落地状态（修复合入前不外推为已生效）**

- 补偿路径的 B 级字段白名单应按**反向动作**取 `writable_fields`（当前取的是正向动作字段，与 `cCode` 不相交），并作为 `narrowed_fields` 传入 `gateway_for`。
- 正向使用 `delete_*` 的 `compensation_only` 拦截尚未落地。
- 上述两项由 S1 修复并二次复核后，本节才视为完全生效。

---

## 5. 与 PRD §2.3 逐条对照

> PRD §2.3 实际为 **9 行**（任务口径说「8 条」，差的一行是「查看计费与价值」，本表按 9 行全列）。

| # | PRD 能力 | PRD 允许角色 | 代码实测 | 一致性 | 冲突编号 |
|---|---|---|---|---|---|
| 1 | 发起 Agent 任务 | 财务对账 / 采购 / 计划车间 | 任何持有 scope 的 `operator` 均可发起并按闸执行；审批人/审计员/管理员被「非发起人」拒绝 | 一致（但按身份而非角色判定） | C4 |
| 2 | 复核 W1 | 财务类 / 采购类 / 生产类 | 已按 `scene → 业务类别 → roles` 限定；未登记场景 fail-closed | 一致（C5 已修复） | — |
| 3 | 审批 W2 / 超阈值 | W2 审批人；>10 万/应付 +复核人；>50 万 +CFO | W2 普通 `approver`；>10 万或触及应付加 `reviewer`；>50 万加 `cfo` | 一致（PRD 已按代码修订） | D1 已关闭 |
| 4 | 回滚自己发起的工单 | 三个业务角色 | 仅要求「会话用户 = 发起人」，无角色限制 | 一致 | — |
| 5 | 回滚他人工单 | IT/CIO（需理由）/ CFO | 策略层已支持：IT/CIO、CFO 带非空理由可回滚他人，其余角色仍拒；理由/回滚人进审计 | 策略一致，HTTP 待接线 | C1 已修复（接线待放行） |
| 6 | 连接器与映射配置 | 仅 IT/CIO | `ConfigGate` 已实现读/写/双人/审计四类判定（§8） | 一致 | C6 已修复 |
| 7 | 查看审计台账 | 本部门 / 全量 | 读/影像角色闸已实现（`TICKET_READ_ROLES`）；「本部门」维度未建模（D3） | 角色一致，部门维度缺失 | C2 已修复（接线待放行） |
| 8 | 导出审计台账 | IT/CIO / CFO / 审计只读 | `AUDIT_EXPORT_ROLES` 仅允许 `platform_admin/cfo/auditor`；导出入审计在 HTTP 层待接线 | 策略一致，HTTP 待接线 | C2 已修复（接线待放行） |
| 9 | 查看计费与价值 | IT/CIO 只读 / CFO / 审计只读 | 无接口；归计量线，等对账落库后由 API 侧做只读视图 | 后置 | C8（后置） |

---

## 6. 冲突清单与处置状态

| 编号 | 问题 | 处置 | 状态 |
|---|---|---|---|
| C1 | 回滚他人工单不可达 | `assert_can_rollback`：IT/CIO、CFO 带非空理由可回滚他人；其余角色仍只限自己发起；理由与回滚人进 `*:rollback_request` 审计影像 | 策略已修复；HTTP 接线待放行 |
| C2 | 读工单/看影像/导出缺角色闸 | `assert_can_read_ticket`、`assert_can_export_audit`；导出仅 IT/CIO、CFO、审计只读；导出留审计在 HTTP 层 | 策略已修复；HTTP 接线待放行 |
| C3 | B 级字段白名单未生效 | `WriteGateway(narrowed_fields=...)` 在 preview 前拒写白名单外字段 | 已修复（测试由红转绿） |
| C4 | B 级工单经 HTTP 不可执行 | API 需下传字段白名单；端到端用例已写好待接线启用 | 待接线（用例 skip，非 xfail） |
| C5 | 复核/审批未按业务类别限定 | `BusinessCategoryPolicy`：`scene → finance/procurement/production`，越类拒绝，未登记 fail-closed | 已修复 |
| C6 | 连接器/映射配置无授权闸口 | `ConfigGate` + `ConfigChangeService`（见 §8） | 已修复（服务层） |
| C7 | `Scope` 缺批次维度 | 已知限制 + 触发条件（§2.3）；登记 OQ-16，客户要求批次授权时走契约变更门 | 接受（POC） |
| C8 | 「查看计费与价值」无实现 | 后置到计量线，等对账落库后由 API 侧做只读视图 | 后置 |
| D1 | PRD 审批行口径错 | 以代码为准修订 PRD §2.3，并注明任务书「8 条」实为 9 行 | 已关闭 |
| D2 | 8 条 vs 9 行 | 同上，PRD 已加口径说明 | 已关闭 |
| D3 | 「本部门」缺部门维度 | 数据模型无 department；与 C2 同源，保留为已知限制 | 接受（POC） |

> C1/C2 的 HTTP 部分与 C4 共用同一条接线（`apps/api/service.py`），按 C1 → C4 → C2 顺序放行。

---

## 7. 证据边界（尽调时主动说明）

1. **不是真实 IAM**：身份模型是进程内 `SessionToken` / `CredentialLease`，未接 OIDC/SAML/mTLS；
   令牌校验、过期、绑定工单已实现，但签名与密钥轮换不在本层。
2. **DB 约束为静态 DDL 断言**：`CHECK`/`UNIQUE` 由 SQLAlchemy 编译验证，未在 PG15 实跑插入；
   迁移与实跑归 S0/T006。
3. **拒绝点可观测**：所有拒绝格断言 `connector.previews == [] and connector.writes == []`，
   即拒绝发生在写闸门之前，不存在「先写后回滚」。
4. **跨租户隔离**：本矩阵固定单租户；跨租户另由 `tests/security/test_idor_scan.py`（17 条路径 18 个探测，双租户同 org/period/system）
   与 `tests/security/test_tenant_query_guard.py`（仓储读方法必须绑定 `tenant_id`）覆盖，租户不匹配一律 404。
5. **未覆盖**：RLS 真实数据库行为、配置变更的 HTTP 入口、计费视图（C8）、批次维度（C7）。
6. **测试与文档同源**：矩阵判定表 `EXPECTED`（测试文件）与本文 §3 一一对应；
   改动任一处都必须同步另一处，否则 `tests/security` 会直接变红。

---

## 8. 连接器与映射配置授权（C6）

实现：`apps/security/config_access.py`（授权/双人判定）+ `apps/security/config_change_chain.py`（PG 落链与验链）；测试：`tests/security/test_config_access.py`（23 条层内）+ `tests/security/test_config_change_pg.py`（9 条真 PG）。

### 8.1 配置矩阵

| 动作 | platform_admin | auditor | 其他业务角色 | 服务账号 |
|---|---|---|---|---|
| 读配置 | ✅ | ✅（密钥字段脱敏 `***`） | ⛔ `ConfigRoleDenied` | ⛔ `ConfigRoleDenied` |
| 改低风险（label/quota） | ✅ 单签 | ⛔ | ⛔ | ⛔ |
| 改高风险（endpoint/credential/field_mapping） | ✅ 需另一名 platform_admin 复核 | ⛔ | ⛔ | ⛔ |
| 变更审计 | 每次变更 1 条 `ConfigAuditEntry` | — | — | — |

### 8.2 关键约束与测试

| 约束 | 测试 |
|---|---|
| 读：IT/CIO 与审计员允许；审计员看到 `***` | `test_c6_config_read_allowed_for_it_and_auditor`、`test_c6_auditor_sees_masked_secrets_only` |
| 读：业务角色与服务账号拒绝 | `test_c6_config_read_denied_for_business_and_service_account` |
| 写：低风险单签即成功且留 1 条审计 | `test_c6_low_risk_change_single_platform_admin` |
| 写：6 类非 IT 角色拒（**含服务账号不得改映射配置**） | `test_c6_config_write_denied_for_non_it_roles` |
| 写：3 类高风险必须另一名 platform_admin 复核 | `test_c6_high_risk_change_requires_second_platform_admin` |
| 写：自审拒、非管理员复核拒 | `test_c6_high_risk_change_rejects_self_approval`、`test_c6_high_risk_change_rejects_non_admin_approver` |
| 未登记 kind 拒 | `test_c6_unknown_change_kind_is_rejected` |
| 每次变更都有 actor/approver 审计 | `test_c6_every_change_is_audited_with_actor_and_approver` |
| DB 约束：`approver_id IS NULL OR approver_id <> actor_id` | `test_c6_config_change_db_check_prevents_self_approval` |

> **本期权限配置为静态（2026-11-11 裁定）**：权限矩阵由代码与本文件固定、部署期生效；本期**不提供运行时变更入口**，也不存在「改完立即生效」的管理界面。
>
> **能力底座已验（B 方案）**：`ConfigChangeService`（授权/双人）→ `SqlConfigChangeRecorder`（写 `erp.config_change`）→ `verify_config_change_chain`（按 `chain_seq` 重放验链）。入口级用例直接调用服务并真跑 PG，见 `tests/security/test_config_change_pg.py`（9 条），覆盖「拒绝零写入、链字段对齐、篡改检出、重复提交不静默去重」。
>
> 管理 API 不在本期（看板 T408 转下期 A 方案）。**环境变量键（`contracts/config.schema.json`，79 键中 78 个 `phase=startup`）不是运行期可写对象**：其变更唯一路径是 `02-并发开发路线.md §4.6` 的 PR → S8 审 → compose 生效；运行时写进去不会改变任何进程行为。

---

## 9. 待接线清单（`apps/api/service.py` 放行前）

以下用例以 `skip` 保留，理由统一为「依赖 `apps/api/service.py` 接线放行」，接线后必须移除：

| 用例 | 目标行为 |
|---|---|
| `test_permission_matrix_cell[initiator__export__same_org]` | 403（导出仅 IT/CIO、CFO、审计） |
| `test_permission_matrix_cell[approver__export__same_org]` | 403 |
| `test_permission_matrix_cell[service_account__export__same_org]` | 403 |
| `test_permission_matrix_cell[service_account__read__same_org]` | 403 |
| `test_permission_matrix_cell[service_account__images__same_org]` | 403 |
| `test_b3_b_level_api_write_fails_closed_without_whitelist_wiring` | 改前留证（401）；接线后删除 |
| `test_c4_b_level_ticket_completes_intent_to_verify_over_http` | 200，`connector_b` 预演/写入/校验各一次 |
