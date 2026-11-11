/**
 * 显式离线 mock（默认关闭，仅 `VITE_USE_MOCK=true` 或测试显式打开时使用）。
 *
 * 数据形状直接引用 C16 生成类型，契约字段变更时这里会类型报错，避免 mock 与真契约脱节。
 * mock 只模拟页面依赖的读接口与关键拒绝路径（W3 403 / 审批未满足 409）。
 */

import type { ApiConfig } from './config';
import type {
  ActionOut,
  ApprovalDecisionOut,
  ApprovalOut,
  ApprovalTaskOut,
  AuditChainOut,
  AuditImageOut,
  MeteringStatementOut,
  PageApprovalTaskOut,
  PageAuditImageOut,
  PageReconBatchSummaryOut,
  PageReconDiffOut,
  PageTicketOut,
  ReconBatchOut,
  ReconBatchSummaryOut,
  ReconDiffDetailOut,
  ReconDiffOut,
  ReconLineResultOut,
  ReconRunIn,
  SessionOut,
  SuggestIn,
  SuggestOut,
  TicketDetailOut,
  TicketOut,
  TicketStepOut,
} from './types';

const TENANT = '11111111-1111-1111-1111-111111111111';
const INITIATOR = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const REVIEWER = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
/** 演示用发起人：让列表里的「发起人」列有区分度，而不是 22 行同一个 aaaaaa…。 */
const INITIATORS = [
  '5f3c1e8a-9d42-4b6e-8a11-6d0f2c7b9e01',
  '7a91c4d2-3e58-4f0b-9c22-8b1e5d6a4f02',
  'c2d7a031-6b94-4e8f-8d33-2f9a7c5e1b03',
];

const T_W3 = 'd0000000-0000-4000-8000-000000000001';
const T_NORMAL = 'd0000000-0000-4000-8000-000000000002';
const T_DUAL = 'd0000000-0000-4000-8000-000000000003';
const T_EXECUTING = 'd0000000-0000-4000-8000-000000000004';
const T_RECON = 'd0000000-0000-4000-8000-000000000005';
const BATCH = 'e0000000-0000-4000-8000-000000000001';
const BATCH_MID = 'e0000000-0000-4000-8000-000000000003';
/** 迁移前的旧批次：无计数投影，汇总字段为 null。 */
const BATCH_OLD = 'e0000000-0000-4000-8000-000000000002';

function ticket(partial: Partial<TicketOut> & Pick<TicketOut, 'ticket_id' | 'ticket_no' | 'state'>): TicketOut {
  return {
    scene_code: 'ag1_reconcile',
    version: 1,
    initiator_id: INITIATORS[(Number(partial.ticket_no.slice(-1)) || 0) % INITIATORS.length],
    business_key: `BK-${partial.ticket_no}`,
    org_id: 'ORG-1',
    period: '2026-10',
    systems: ['u8', 'statement'],
    write_level: 'W1',
    amount: '1200.0000',
    touches_fund: false,
    created_at: '2026-11-10T09:00:00Z',
    updated_at: '2026-11-10T09:30:00Z',
    ...partial,
  };
}

/**
 * 演示数据集：22 张工单覆盖 12 个状态、W1/W2/W3、含/不含资金，金额均为真实量级。
 * 只服务 VITE_USE_MOCK 通道，字段形状仍由 C16 生成类型约束。
 */
const TICKETS: TicketOut[] = [
  ticket({
    ticket_id: T_NORMAL,
    ticket_no: 'T-20261111-001',
    state: '待审批',
    business_key: '采购入库-华晟精密-2026-10',
    amount: '12480.0000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'statement'],
    created_at: '2026-11-11T01:20:00Z',
    updated_at: '2026-11-11T01:42:00Z',
  }),
  ticket({
    ticket_id: T_DUAL,
    ticket_no: 'T-20261111-002',
    state: '待审批',
    write_level: 'W2',
    business_key: '委外加工-恒信机电-2026-10',
    amount: '286000.0000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'mes', 'statement'],
    created_at: '2026-11-11T01:05:00Z',
    updated_at: '2026-11-11T01:38:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000006',
    ticket_no: 'T-20261111-003',
    state: '待审批',
    business_key: '销售出库-瑞禾商贸-2026-10',
    amount: '56200.0000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'statement'],
    created_at: '2026-11-11T00:58:00Z',
    updated_at: '2026-11-11T01:30:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000007',
    ticket_no: 'T-20261111-004',
    state: '待审批',
    write_level: 'W2',
    business_key: '费用分摊-行政-2026-10',
    amount: '38400.0000',
    scene_code: 'ag1_reconcile',
    systems: ['u8'],
    created_at: '2026-11-11T00:45:00Z',
    updated_at: '2026-11-11T01:12:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000008',
    ticket_no: 'T-20261111-005',
    state: '待审批',
    business_key: '采购入库-盛达五金-2026-10',
    amount: '8675.5000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'wms'],
    created_at: '2026-11-11T00:30:00Z',
    updated_at: '2026-11-11T01:05:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000009',
    ticket_no: 'T-20261111-006',
    state: '待审批',
    business_key: '报废冲减-华晟精密-2026-10',
    amount: '4320.5000',
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'mes'],
    created_at: '2026-11-11T00:18:00Z',
    updated_at: '2026-11-11T00:52:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000010',
    ticket_no: 'T-20261111-007',
    state: '待审批',
    write_level: 'W2',
    business_key: '价格调整-恒信机电-2026-11',
    amount: '128000.0000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8', 'statement'],
    created_at: '2026-11-10T23:50:00Z',
    updated_at: '2026-11-11T00:40:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000011',
    ticket_no: 'T-20261111-008',
    state: '待审批',
    business_key: '暂估冲回-瑞禾商贸-2026-10',
    amount: '19760.0000',
    touches_fund: true,
    scene_code: 'ag1_reconcile',
    systems: ['u8'],
    created_at: '2026-11-10T23:36:00Z',
    updated_at: '2026-11-11T00:24:00Z',
  }),
  ticket({
    ticket_id: T_W3,
    ticket_no: 'T-20261111-009',
    state: '待审批',
    write_level: 'W3',
    business_key: '期末结账-总账-2026-10',
    amount: '640000.0000',
    scene_code: 'ag1_reconcile',
    systems: ['u8'],
    created_at: '2026-11-10T23:10:00Z',
    updated_at: '2026-11-11T00:05:00Z',
  }),
  ticket({
    ticket_id: T_EXECUTING,
    ticket_no: 'T-20261110-010',
    state: '执行中',
    business_key: '采购入库-盛达五金-2026-10',
    amount: '21800.0000',
    touches_fund: true,
    systems: ['u8', 'wms'],
    created_at: '2026-11-10T08:40:00Z',
    updated_at: '2026-11-11T00:02:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000012',
    ticket_no: 'T-20261110-011',
    state: '执行中',
    write_level: 'W2',
    business_key: '委外加工-华晟精密-2026-10',
    amount: '96400.0000',
    touches_fund: true,
    systems: ['u8', 'mes'],
    created_at: '2026-11-10T08:12:00Z',
    updated_at: '2026-11-10T23:48:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000013',
    ticket_no: 'T-20261110-012',
    state: '校验中',
    business_key: '销售出库-瑞禾商贸-2026-10',
    amount: '7340.0000',
    touches_fund: true,
    systems: ['u8', 'statement'],
    created_at: '2026-11-10T07:55:00Z',
    updated_at: '2026-11-10T23:20:00Z',
  }),
  ticket({
    ticket_id: T_RECON,
    ticket_no: 'T-20261110-013',
    state: '已完成',
    business_key: '采购入库-恒信机电-2026-10',
    amount: '15800.0000',
    touches_fund: true,
    systems: ['u8', 'wms'],
    created_at: '2026-11-10T06:30:00Z',
    updated_at: '2026-11-10T22:10:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000014',
    ticket_no: 'T-20261110-014',
    state: '已完成',
    write_level: 'W2',
    business_key: '费用分摊-行政-2026-10',
    amount: '26400.0000',
    systems: ['u8'],
    created_at: '2026-11-10T06:05:00Z',
    updated_at: '2026-11-10T21:36:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000015',
    ticket_no: 'T-20261109-015',
    state: '补偿中',
    business_key: '报废冲减-盛达五金-2026-10',
    amount: '3120.0000',
    systems: ['u8', 'mes'],
    created_at: '2026-11-09T09:20:00Z',
    updated_at: '2026-11-10T20:48:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000016',
    ticket_no: 'T-20261109-016',
    state: '已回滚',
    business_key: '价格调整-华晟精密-2026-10',
    amount: '9600.0000',
    touches_fund: true,
    systems: ['u8', 'statement'],
    created_at: '2026-11-09T08:44:00Z',
    updated_at: '2026-11-10T19:22:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000017',
    ticket_no: 'T-20261109-017',
    state: '需人工处置',
    write_level: 'W2',
    business_key: '暂估冲回-恒信机电-2026-10',
    amount: '45800.0000',
    touches_fund: true,
    systems: ['u8', 'statement'],
    created_at: '2026-11-09T08:10:00Z',
    updated_at: '2026-11-10T18:40:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000018',
    ticket_no: 'T-20261108-018',
    state: '已锁定',
    write_level: 'W3',
    business_key: '期末结账-总账-2026-10',
    amount: '1280000.0000',
    systems: ['u8'],
    created_at: '2026-11-08T10:00:00Z',
    updated_at: '2026-11-10T17:15:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000019',
    ticket_no: 'T-20261108-019',
    state: '已终止',
    business_key: '采购入库-瑞禾商贸-2026-10',
    amount: '5400.0000',
    systems: ['u8', 'wms'],
    created_at: '2026-11-08T09:12:00Z',
    updated_at: '2026-11-10T16:02:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000020',
    ticket_no: 'T-20261108-020',
    state: '已驳回',
    business_key: '销售出库-华晟精密-2026-10',
    amount: '12900.0000',
    touches_fund: true,
    systems: ['u8', 'statement'],
    created_at: '2026-11-08T08:20:00Z',
    updated_at: '2026-11-10T15:30:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000021',
    ticket_no: 'T-20261107-021',
    state: '计划中',
    business_key: '采购计划-恒信机电-2026-11',
    amount: '32000.0000',
    systems: ['u8'],
    period: '2026-11',
    created_at: '2026-11-07T09:00:00Z',
    updated_at: '2026-11-10T14:12:00Z',
  }),
  ticket({
    ticket_id: 'd0000000-0000-4000-8000-000000000022',
    ticket_no: 'T-20261107-022',
    state: '待确认',
    business_key: '委外加工-盛达五金-2026-10',
    amount: '68800.0000',
    touches_fund: true,
    systems: ['u8', 'mes'],
    created_at: '2026-11-07T08:36:00Z',
    updated_at: '2026-11-10T13:48:00Z',
  }),
];

const TICKET_INDEX = new Map(TICKETS.map((row, index) => [row.ticket_id, index]));

function at(iso: string, minutes: number): string {
  return new Date(new Date(iso).getTime() + minutes * 60_000).toISOString();
}

/** 状态 → 步骤状态序列；与 state_machine.v1.json 的推进顺序对齐，供详情页时间线展示。 */
const STEP_STATES: Record<string, string[]> = {
  计划中: [],
  待确认: ['intent'],
  待审批: ['intent'],
  执行中: ['succeeded', 'running'],
  校验中: ['succeeded', 'succeeded', 'running'],
  已完成: ['succeeded', 'succeeded', 'succeeded'],
  补偿中: ['succeeded', 'succeeded', 'compensating'],
  已回滚: ['succeeded', 'compensated', 'compensated'],
  需人工处置: ['succeeded', 'failed', 'unresolved'],
  已锁定: ['succeeded', 'failed'],
  已终止: ['skipped'],
  已驳回: ['intent'],
};

const STEP_TEMPLATES = [
  { action_key: 'u8.trade.save_draft', system: 'u8', object_type: 'draft', prefix: 'D' },
  { action_key: 'u8.trade.submit', system: 'u8', object_type: 'draft', prefix: 'D' },
  { action_key: 'u8.voucher.create', system: 'u8', object_type: 'voucher', prefix: 'V' },
];

function stepsFor(row: TicketOut): TicketStepOut[] {
  const states = STEP_STATES[row.state] ?? [];
  return states.map((state, index) => {
    const template = STEP_TEMPLATES[index] ?? STEP_TEMPLATES[0];
    const stepNo = index + 1;
    return {
      step_no: stepNo,
      step_id: `f0000000-0000-4000-8000-${String((TICKET_INDEX.get(row.ticket_id) ?? 0) * 10 + stepNo).padStart(12, '0')}`,
      action_key: template.action_key,
      system: template.system,
      object_type: template.object_type,
      object_id: `${template.prefix}-${row.ticket_no.slice(-3)}`,
      state,
      level: row.write_level,
      idem_key: `idem-${row.ticket_no.toLowerCase()}-${stepNo}`,
      executed_at: state === 'intent' || state === 'skipped' ? null : at(row.created_at, stepNo * 6),
    };
  });
}

/** 只给已执行（succeeded/compensated/rolled_back）的步骤产出审计影像，避免「待审批却有写入证据」。 */
function imagesFor(row: TicketOut): AuditImageOut[] {
  const steps = stepsFor(row).filter((step) => ['succeeded', 'compensated', 'rolled_back'].includes(step.state));
  const gross = Number(row.amount);
  const before = (gross * 0.92).toFixed(4);
  return steps.map((step, index) => {
    const qty = 100 + index * 12;
    const indexInList = (TICKET_INDEX.get(row.ticket_id) ?? 0) * 10 + index + 1;
    return {
      image_id: `a0000000-0000-4000-8000-${String(indexInList).padStart(12, '0')}`,
      ticket_id: row.ticket_id,
      step_id: step.step_id,
      system: step.system,
      object_type: step.object_type,
      object_id: step.object_id,
      before_ref: `mem://before/${row.ticket_no}`,
      after_ref: `mem://after/${row.ticket_no}`,
      field_diff:
        index === 0
          ? [
              { field: 'qty', label: '数量', before: qty, after: qty + 12, kind: 'qty', unit: '件' },
              { field: 'amount', label: '金额', before, after: row.amount, kind: 'amount' },
            ]
          : [{ field: 'amount', label: '金额', before, after: row.amount, kind: 'amount' }],
      diff_missing: false,
      prev_hash: index === 0 ? 'GENESIS' : `sha256:mock-${row.ticket_no}-${index}`,
      hash: `sha256:mock-${row.ticket_no}-${index + 1}`,
      created_at: step.executed_at ?? row.updated_at,
    };
  });
}

function approvalsFor(row: TicketOut, required: string[]): ApprovalOut[] {
  if (row.state === '待审批' && row.ticket_id === T_DUAL) {
    return [
      {
        role: 'reviewer',
        approver_id: REVIEWER,
        decision: 'approved',
        comment: '复核通过（该意见不得展示给其他审批人）',
        decided_at: at(row.created_at, 25),
      },
    ];
  }
  if (row.state === '已驳回') {
    return [
      {
        role: 'reviewer',
        approver_id: REVIEWER,
        decision: 'rejected',
        comment: '对账差异未说明形成原因，退回补充凭证后重提',
        decided_at: at(row.updated_at, -30),
      },
    ];
  }
  if (['执行中', '校验中', '已完成', '补偿中', '已回滚', '需人工处置', '已锁定'].includes(row.state)) {
    const roles = required.length > 0 ? required : ['reviewer'];
    return roles.map((role, index) => ({
      role,
      approver_id: index === 0 ? REVIEWER : INITIATOR,
      decision: 'approved' as const,
      comment: index === 0 ? '复核通过：差异与凭证一致' : '审批通过，按对账结论执行',
      decided_at: at(row.created_at, 20 + index * 8),
    }));
  }
  return [];
}

function detail(row: TicketOut): TicketDetailOut {
  const required = row.write_level === 'W3' ? [] : row.write_level === 'W2' ? ['reviewer', 'approver'] : ['reviewer'];
  const approvals = approvalsFor(row, required);
  const approved = new Set(approvals.filter((item) => item.decision === 'approved').map((item) => item.role));
  const pending = row.state === '待审批' || row.state === '待确认';
  const missing = pending ? required.filter((role) => !approved.has(role)) : [];
  return {
    ...row,
    steps: stepsFor(row),
    approvals,
    approval_required_roles: required,
    approval_missing_roles: missing,
    // mock 连接器按 A 级处理：无强制双签，执行组与审批组一致。
    execution_required_roles: required,
    execution_missing_roles: missing,
    audit_image_count: imagesFor(row).length,
    // 与后端默认一致：同步回滚入口关闭，前端据此禁用按钮。
    rollback_available: false,
  };
}

const METRICS: ReconBatchOut['metrics'] = {
  supplier_lines: 192,
  matched: 178,
  matched_rate: '0.927083',
  ambiguous: 3,
  candidate_only: 2,
  unmatched: 9,
  diff_lines: 10,
  diff_rate: '0.052083',
  diff_amount: '113242.8300',
  suspected_anomaly: false,
  auto_disposal_allowed: true,
};

/** 10 条差异覆盖契约 DiffTypeCode 的全部 8 类，供批次页筛选与差异页证据链演示。 */
const DIFFS: ReconDiffOut[] = [
  {
    diff_id: 'd0',
    type: 'qty',
    billing_bucket: 'other_unbillable',
    amount: { amount: '-28900.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '数量差额需与入库单/报工单核对，确认后补单或在下期冲抵',
    confidence: '0.88',
    needs_manual: false,
    ref: 'statement:ST-202610-001#1',
    supplier_code: 'S-001',
    evidence: [
      { system: 'u8', doc_no: 'PO-202610-001', field: 'qty', coord: { page: 1, bbox: [72, 138, 210, 152] } },
      { system: 'statement', doc_no: 'ST-202610-001', field: 'qty', coord: { page: 2, bbox: [80, 210, 260, 226] } },
    ],
  },
  {
    diff_id: 'd1',
    type: 'unit_price',
    billing_bucket: 'dispute_hold',
    amount: { amount: '12480.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '我方单价 12.50 与对账单 13.20 不一致，核对价格协议与调价生效日期',
    confidence: '0.9',
    needs_manual: true,
    ref: 'statement:ST-202610-001#2',
    supplier_code: 'S-001',
    evidence: [
      { system: 'u8', doc_no: 'PO-202610-001', field: 'unit_price', coord: { page: 1, bbox: [300, 212, 380, 226] } },
      { system: 'statement', doc_no: 'ST-202610-001', field: 'unit_price', coord: { page: 2, bbox: [300, 212, 380, 226] } },
    ],
  },
  {
    diff_id: 'd2',
    type: 'rounding_tolerance',
    billing_bucket: 'auto_released',
    amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
    owner: 'system',
    suggestion: '数量/单价一致，金额差 0.03 元在舍入容差 0.05 元内，按容差放行',
    confidence: '0.95',
    needs_manual: false,
    ref: 'statement:ST-202610-002#1',
    supplier_code: 'S-002',
    evidence: [],
  },
  {
    diff_id: 'd3',
    type: 'scrap',
    billing_bucket: 'other_unbillable',
    amount: { amount: '-4320.5000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '报废 12 件未在结算中扣除，按报工单 WO-202610-118 扣减',
    confidence: '0.86',
    needs_manual: false,
    ref: 'mes:WO-202610-118#3',
    supplier_code: 'S-002',
    evidence: [
      { system: 'mes', doc_no: 'WO-202610-118', field: 'scrap_qty', coord: { page: 1, bbox: [64, 320, 180, 336] } },
    ],
  },
  {
    diff_id: 'd4',
    type: 'uom',
    billing_bucket: 'dispute_hold',
    amount: { amount: '-1860.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '对账单按「箱」计价，订单按「件」计价，换算系数 1 箱 = 24 件需确认',
    confidence: '0.81',
    needs_manual: true,
    ref: 'statement:ST-202610-003#4',
    supplier_code: 'S-003',
    evidence: [
      { system: 'u8', doc_no: 'PO-202610-031', field: 'unit', coord: { page: 1, bbox: [120, 244, 196, 258] } },
      { system: 'statement', doc_no: 'ST-202610-003', field: 'unit', coord: { page: 3, bbox: [132, 250, 208, 264] } },
    ],
  },
  {
    diff_id: 'd5',
    type: 'tax',
    billing_bucket: 'dispute_hold',
    amount: { amount: '7642.3000', currency: 'CNY', tax_included: true },
    owner: 'finance',
    suggestion: '对账单按 13% 税率开票，合同约定 9%，差额需供应商换票',
    confidence: '0.92',
    needs_manual: true,
    ref: 'statement:ST-202610-004#2',
    supplier_code: 'S-004',
    evidence: [
      { system: 'statement', doc_no: 'ST-202610-004', field: 'tax_rate', coord: { page: 2, bbox: [412, 286, 468, 300] } },
    ],
  },
  {
    diff_id: 'd6',
    type: 'missing_doc',
    billing_bucket: 'other_unbillable',
    amount: { amount: '-18600.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '对账单列示送货单 DN-202610-2207，但 ERP 无对应入库单，需补录或剔除',
    confidence: '0.89',
    needs_manual: true,
    ref: 'statement:ST-202610-004#7',
    supplier_code: 'S-004',
    evidence: [
      { system: 'statement', doc_no: 'ST-202610-004', field: 'doc_no', coord: { page: 4, bbox: [80, 402, 236, 418] } },
    ],
  },
  {
    diff_id: 'd7',
    type: 'duplicate',
    billing_bucket: 'other_unbillable',
    amount: { amount: '-23400.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '同一送货单 DN-202610-1188 在 10 月与 9 月对账单重复计费，本期应剔除',
    confidence: '0.94',
    needs_manual: false,
    ref: 'statement:ST-202610-005#2',
    supplier_code: 'S-005',
    evidence: [
      { system: 'statement', doc_no: 'ST-202610-005', field: 'doc_no', coord: { page: 2, bbox: [96, 268, 252, 284] } },
      { system: 'statement', doc_no: 'ST-202609-011', field: 'doc_no', coord: { page: 2, bbox: [96, 268, 252, 284] } },
    ],
  },
  {
    diff_id: 'd8',
    type: 'unit_price',
    billing_bucket: 'dispute_hold',
    amount: { amount: '-9800.0000', currency: 'CNY', tax_included: true },
    owner: 'buyer',
    suggestion: '采购返利未在单价中抵扣，按协议返利 2% 重算单价',
    confidence: '0.77',
    needs_manual: true,
    ref: 'statement:ST-202610-005#6',
    supplier_code: 'S-005',
    evidence: [
      { system: 'u8', doc_no: 'PO-202610-052', field: 'unit_price', coord: { page: 1, bbox: [310, 198, 390, 212] } },
    ],
  },
  {
    diff_id: 'd9',
    type: 'qty',
    billing_bucket: 'billable',
    amount: { amount: '6240.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '我方漏记退货 8 件，对账单数量更低，确认后调整应付',
    confidence: '0.9',
    needs_manual: false,
    ref: 'statement:ST-202610-006#3',
    supplier_code: 'S-006',
    evidence: [
      { system: 'u8', doc_no: 'PO-202610-066', field: 'qty', coord: { page: 1, bbox: [156, 176, 260, 190] } },
      { system: 'statement', doc_no: 'ST-202610-006', field: 'qty', coord: { page: 1, bbox: [156, 176, 260, 190] } },
    ],
  },
];

const RELATED_LINES: ReconLineResultOut[] = [
  {
    item: 'A-001',
    qty_mine: '100',
    qty_supplier: '112',
    amount: { amount: '-28900.0000', currency: 'CNY', tax_included: true },
    match_type: 'single',
    confidence: '0.88',
    status: 'diff',
    our_ref: 'u8:PO-202610-001#1',
    supplier_ref: 'statement:ST-202610-001#1',
    needs_manual: false,
    note: '数量相差 12 件',
  },
  {
    item: 'A-002',
    qty_mine: '24',
    qty_supplier: '24',
    amount: { amount: '12480.0000', currency: 'CNY', tax_included: true },
    match_type: 'single',
    confidence: '0.9',
    status: 'diff',
    our_ref: 'u8:PO-202610-001#2',
    supplier_ref: 'statement:ST-202610-001#2',
    needs_manual: true,
    note: '单价相差 0.70 元/件',
  },
  {
    item: 'A-003',
    qty_mine: '60',
    qty_supplier: '60',
    amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
    match_type: 'single',
    confidence: '0.95',
    status: 'diff',
    our_ref: 'u8:PO-202610-002#1',
    supplier_ref: 'statement:ST-202610-002#1',
    needs_manual: false,
    note: '金额舍入差 0.03 元',
  },
];

function pendingTasks(): ApprovalTaskOut[] {
  return TICKETS.filter((row) => row.state === '待审批' && row.write_level !== 'W3').map((row) => {
    const required = row.write_level === 'W2' ? ['reviewer', 'approver'] : ['reviewer'];
    return {
      ticket_id: row.ticket_id,
      ticket_no: row.ticket_no,
      scene_code: row.scene_code,
      business_key: row.business_key,
      state: row.state,
      write_level: row.write_level,
      amount: row.amount,
      touches_fund: row.touches_fund,
      initiator_id: row.initiator_id,
      approval_required_roles: required,
      approval_missing_roles: row.ticket_id === T_DUAL ? ['approver'] : required,
      execution_required_roles: required,
      execution_missing_roles: row.ticket_id === T_DUAL ? ['approver'] : required,
      created_at: row.created_at,
    };
  });
}

function problem(status: number, code: string, detail: string, extra: Record<string, unknown> = {}) {
  return {
    status,
    body: {
      type: 'about:blank',
      title: status === 403 ? 'Forbidden' : status === 409 ? 'Conflict' : 'Bad Request',
      status,
      detail,
      code,
      trace_id: 'mock-trace',
      retriable: false,
      ...extra,
    },
  };
}

function findTicket(ticketId: string): TicketOut {
  const row = TICKETS.find((item) => item.ticket_id === ticketId);
  if (!row) throw new Error('TICKET_NOT_FOUND');
  return row;
}

function action(ticketId: string, kind: string, body: Record<string, unknown>) {
  const row = findTicket(ticketId);
  if (kind === 'approve') {
    if (row.write_level === 'W3') {
      return problem(403, 'E-POLICY-W3FORBIDDEN', 'W3 写等级禁止自动执行，整单拒绝；连接器零写入');
    }
    if (row.state !== '待审批') {
      return problem(409, 'E-STATE-ILLEGAL', `工单 ${row.ticket_no} 当前为 ${row.state}，只有待审批可以审批通过`);
    }
    row.state = '执行中';
    row.version += 1;
    const out: ActionOut = {
      ticket_id: row.ticket_id,
      action: 'approve',
      state: row.state,
      version: row.version,
      approval_required_roles: [],
      approval_missing_roles: [],
      execution_required_roles: [],
      execution_missing_roles: [],
      executed_steps: stepsFor(row),
      manual_tasks: [],
    };
    return { status: 200, body: out };
  }
  if (kind === 'reject') {
    if (!body.role) return problem(409, 'E-POLICY-APPROVAL', '驳回必须指定审批角色（role）');
    if (row.state !== '待审批') return problem(409, 'E-STATE-ILLEGAL', '只有待审批可以驳回');
    row.state = '已驳回';
    row.version += 1;
    return {
      status: 200,
      body: { ticket_id: row.ticket_id, action: 'reject', state: row.state, version: row.version },
    };
  }
  if (kind === 'cancel') {
    if (!String(body.comment ?? '').trim()) return problem(422, 'E-DATA-MISSING', '终止工单必须写明原因（comment）');
    if (!['计划中', '需人工处置', '待审批'].includes(row.state)) {
      return problem(409, 'E-STATE-ILLEGAL', `工单当前为 ${row.state}，不允许终止`);
    }
    row.state = '已终止';
    row.version += 1;
    return { status: 200, body: { ticket_id: row.ticket_id, action: 'cancel', state: row.state, version: row.version } };
  }
  if (kind === 'compensate') {
    return problem(
      503,
      'E-ROLLBACK-UNAVAILABLE',
      '同步回滚入口已关闭（ERP_ROLLBACK_SYNC_ENABLED 未开启）：本期只读，未调用任何连接器',
    );
  }
  return problem(422, 'E-PARSE-FAILED', '未知动作');
}

function decide(ticketId: string, decision: 'approve' | 'reject', body: Record<string, unknown>) {
  const row = findTicket(ticketId);
  if (row.state !== '待审批') return problem(409, 'E-STATE-ILLEGAL', '不在待审批');
  if (!body.role) return problem(409, 'E-POLICY-APPROVAL', '审批角色必填');
  if (row.ticket_id === T_DUAL && body.role !== 'approver') {
    return problem(409, 'E-POLICY-APPROVAL', '审批未满足：还需 approver（财务/业务负责人）审批', {
      approval_missing_roles: ['approver'],
      approval_required_roles: ['reviewer', 'approver'],
    });
  }
  if (decision === 'reject' && !String(body.comment ?? '').trim()) {
    return problem(409, 'E-POLICY-APPROVAL', '驳回意见必填');
  }
  if (decision === 'reject') {
    row.state = '已驳回';
    row.version += 1;
  }
  const out: ApprovalDecisionOut = {
    ticket_id: row.ticket_id,
    role: String(body.role),
    decision: decision === 'approve' ? 'approved' : 'rejected',
    decided_at: new Date().toISOString(),
    satisfied: row.ticket_id !== T_DUAL || body.role === 'approver',
    approval_required_roles: row.ticket_id === T_DUAL ? ['reviewer', 'approver'] : ['reviewer'],
    approval_missing_roles: row.ticket_id === T_DUAL && body.role !== 'approver' ? ['approver'] : [],
    execution_required_roles: row.ticket_id === T_DUAL ? ['reviewer', 'approver'] : ['reviewer'],
    execution_missing_roles: row.ticket_id === T_DUAL && body.role !== 'approver' ? ['approver'] : [],
    ticket_state: row.state,
  };
  return { status: 200, body: out };
}

function batchOut(batchId: string = BATCH): ReconBatchOut {
  if (batchId === BATCH_OLD) {
    // 迁移前的旧批次：服务端没有计数投影，三个汇总字段为 null（不是 0）。
    return {
      batch_id: batchId,
      status: 'completed',
      conclusive: false,
      status_reason: '旧批次：计数投影在迁移前未回填，汇总字段为 null；明细可下钻但不对账汇总',
      org_id: 'ORG-1',
      period: '2026-09',
      system_ids: ['u8'],
      suppliers: ['S-001'],
      metrics: null,
      missing_items: [],
      created_by: INITIATOR,
      created_at: '2026-09-30T16:20:00Z',
    };
  }
  return {
    batch_id: batchId,
    status: 'reconciled',
    conclusive: true,
    status_reason: '全部供应商对账单已核完，差异均在可处置范围',
    org_id: 'ORG-1',
    period: '2026-10',
    system_ids: ['u8', 'statement'],
    suppliers: ['S-001', 'S-002', 'S-003', 'S-004', 'S-005', 'S-006'],
    metrics: METRICS,
    missing_items: [],
    diff_count: DIFFS.length,
    created_by: INITIATOR,
    created_at: '2026-11-10T10:00:00Z',
  };
}

/**
 * 批次列表的离线样本：含一条没有投影的旧批次（三个汇总字段为 null），
 * 用来在 demo 里验证「null 不能渲染成 0」。
 */
const BATCH_SUMMARIES: ReconBatchSummaryOut[] = [
  {
    batch_id: BATCH,
    status: 'reconciled',
    line_count: 192,
    diff_count: DIFFS.length,
    diff_amount: '113242.8300',
    created_at: '2026-11-10T10:00:00Z',
  },
  {
    batch_id: BATCH_MID,
    status: 'reconciled',
    line_count: 64,
    diff_count: 2,
    diff_amount: '0.0300',
    created_at: '2026-10-31T09:12:00Z',
  },
  {
    batch_id: BATCH_OLD,
    status: 'completed',
    line_count: null,
    diff_count: null,
    diff_amount: null,
    created_at: '2026-09-30T16:20:00Z',
  },
];

function diffDetail(diffId: string): ReconDiffDetailOut {
  const base = DIFFS.find((item) => item.diff_id === diffId);
  if (!base) throw new Error('DIFF_NOT_FOUND');
  return {
    ...base,
    batch_id: BATCH,
    related_lines: RELATED_LINES,
    draft: {
      draft_no: 'DT-MOCK-001',
      supplier_code: base.supplier_code,
      period: '2026-10',
      diff_type: base.type,
      amount: base.amount,
      owner: base.owner,
      suggestion: base.suggestion,
      confidence: base.confidence,
      needs_manual: base.needs_manual,
      refs: [base.ref],
      status: '草稿',
    },
    unattributed: null,
  };
}

/** 与 expected_lines=64、四桶 59+0+3+2 完全一致的完整明细，保证卡片数字 = 下钻行数。 */
function meteringLine(
  line_no: number,
  bucket: 'billable' | 'auto_released' | 'dispute_hold' | 'other_unbillable',
): NonNullable<MeteringStatementOut['lines']>[number] {
  const billable = bucket === 'billable';
  const dispute = bucket === 'dispute_hold';
  const ref = String(line_no).padStart(3, '0');
  return {
    line_no,
    side: 'supplier',
    status: billable ? '已对账' : dispute ? '差异挂起' : '未匹配',
    billing_bucket: bucket,
    auto_released: billable || bucket === 'auto_released',
    needs_manual: dispute,
    confidence: billable ? '0.98' : dispute ? '0.72' : '0.55',
    ticket_no: `T-${1000 + line_no}`,
    supplier_code: 'S-001',
    owner: billable ? 'system' : 'supplier',
    our_ref: `u8:PO-202610-${ref}#1`,
    supplier_ref: `statement:ST-202610-${ref}#1`,
    before_ref: null,
    after_ref: billable ? `audit:image-${line_no}` : null,
    human_modified: null,
    diff_types: billable ? [] : ['rounding_tolerance'],
    source_refs: [`statement:ST-202610-${ref}#1`],
    evidence: [],
    excluded_reason: billable ? '' : dispute ? '差异未确认，挂起不计费' : '未匹配，不计费',
  };
}

function meteringLines(): NonNullable<MeteringStatementOut['lines']> {
  return [
    ...Array.from({ length: 59 }, (_, index) => meteringLine(index + 1, 'billable')),
    ...Array.from({ length: 3 }, (_, index) => meteringLine(60 + index, 'dispute_hold')),
    ...Array.from({ length: 2 }, (_, index) => meteringLine(63 + index, 'other_unbillable')),
  ];
}

function metering(): MeteringStatementOut {
  return {
    batch_id: BATCH,
    batch_status: '已完成',
    conclusive: true,
    period: '2026-10',
    supplier_scope: ['S-001'],
    expected_lines: 64,
    auto_released_lines: 59,
    billable_lines: 59,
    billable_basis:
      'billable_lines = auto_released_lines − confirmed_misrelease_lines − customer_reason_lines；后两项当前无数据源，按 0 扣减，故为可计费上界',
    excluded_lines: { total: 5, dispute_hold: 3, other_unbillable: 2, misrelease: null, customer_reason: null, other_reasons: { 重复单据: 2 } },
    unit_price: '0.8000',
    unit_price_source: 'ERP_METERING_UNIT_PRICE',
    currency: 'CNY',
    period_amount: '47.2000',
    prior_cumulative_amount: '120.0000',
    cumulative_amount: '167.2000',
    cap_amount: null,
    cap_remaining: null,
    cap_exceeded: false,
    confirmed_misrelease_lines: null,
    customer_reason_lines: null,
    human_reviewed_lines: null,
    line_accounting: '应处理 64 = 自动放行 59 + 争议挂起 3 + 其他不计费 2（明细行合计 64）',
    calculation: [
      '应处理行数 = 供应商对账单行数 = 64（来源：对账引擎 metrics.supplier_lines）',
      '自动放行行数 = 引擎状态「已匹配」且无需人工 = 59',
      '争议挂起行数 = 引擎标记需人工（待人工/匹配歧义/候选待人工）= 3',
      '其他不计费行数 = 供应商侧未匹配/重复计费/不可解析 = 2',
      '计费桶 = billable 59 + auto_released 0 + dispute_hold 3 + other_unbillable 2 = 明细行数 64（逐行 billing_bucket 与计数同源）',
      '已确认错放行行数 = 无数据源（未接入，按 0 扣减）；客户原因不计费行数 = 无数据源（未接入，按 0 扣减）',
      // 服务端已分离三个行数，mock 只做镜像：有效对账行 = billable 桶 + auto_released 桶；
      // 计费行数 = billable 桶行数（扣减源未接入，按 0 扣减，故为上界）。
      '总对账行数（line_count，批次内全部行）= 64',
      '有效对账行数（auto_released_lines，行级 auto）= billable 桶 59 + auto_released 桶 0 = 59',
      '计费行数（billable_lines，billable 桶行数）= 59（上界：扣减源接入后只会下调）',
      '本期金额 = 计费行数 59 × 单行单价 0.8000 = 47.2000 CNY',
      '累计金额 = 已计金额 120.0000 + 本期金额 47.2000 = 167.2000',
      '未配置封顶金额（ERP_METERING_CAP_AMOUNT），封顶余额不可核对',
    ],
    data_gaps: [
      {
        field: 'cap_amount',
        reason: '合同封顶金额未进入数据模型，无法计算封顶余额',
        owner_role: '商务/合同管理员',
        action: '提供合同封顶金额与生效区间，供计量计算封顶余额',
      },
      {
        field: 'confirmed_misrelease_lines',
        reason: '错放行确认结果当前无数据源',
        owner_role: '对账运营',
        action: '人工复核结案后回填已确认错放行的行号与凭证，再据此扣减有效行',
      },
      {
        field: 'customer_reason_lines',
        reason: '客户原因分类当前无数据源',
        owner_role: '客户成功/商务',
        action: '回填客户原因不计费的对账行、原因码与确认人，供计量剔除',
      },
    ],
    computed_at: '2026-11-10T12:00:00Z',
    line_count: 64,
    lines: meteringLines(),
  };
}

function chain(): AuditChainOut {
  return {
    ticket_id: T_NORMAL,
    tenant_id: TENANT,
    ok: true,
    checked: 2,
    broken_at: null,
    detail: '哈希链完整',
    image_count: 2,
    head_hash: 'sha256:mock-1007',
  };
}

/**
 * 助手建议离线样本。
 *
 * 两种 `source` 都要能在本地看到：W3 工单（T-20261111-009）固定走 rule + fallback_reason，
 * 其余工单走 llm。rule 建议文案与后端 `_rule_suggestion` 的 W3 分支一致，避免 mock 教坏页面。
 */
function assistantSuggestion(row: TicketOut): SuggestOut {
  if (row.write_level === 'W3') {
    return {
      suggestion: 'W3 动作禁止自动执行，转人工或线下处理。',
      reasons: [`工单当前状态：${row.state}`, '写等级：W3', 'W3 无自动执行路径'],
      source: 'rule',
      model: 'rule/next_action-v1',
      generated_at: new Date().toISOString(),
      fallback_reason: 'llm_not_configured',
    };
  }
  return {
    suggestion: '（离线 mock）请按工单当前状态核对审批与执行回执，再决定下一步。',
    reasons: [
      `工单当前状态：${row.state}`,
      `写等级：${row.write_level}`,
      row.touches_fund ? '涉及资金，审批与校验需留痕' : '不涉及资金，按状态机留痕',
    ],
    source: 'llm',
    model: 'mock-assistant-v0',
    generated_at: new Date().toISOString(),
    fallback_reason: null,
  };
}

export interface MockResponse {
  status: number;
  body?: unknown;
  text?: string;
  headers?: Record<string, string>;
}

/** mock 路由入口：与真实 client 的请求参数一致，返回 HTTP 形状供 client 统一处理。 */
export async function mockSend(
  method: string,
  path: string,
  query: Record<string, unknown>,
  body: unknown,
  _config: ApiConfig,
): Promise<MockResponse> {
  await new Promise((resolve) => setTimeout(resolve, 30));
  const parts = path.replace(/^\/v1\//, '').split('/');
  const q = query as Record<string, string | string[] | undefined>;

  if (method === 'GET' && path === '/v1/auth/session') {
    return {
      status: 200,
      body: {
        user_id: REVIEWER,
        tenant_id: TENANT,
        roles: ['reviewer', 'approver'],
        // 与契约一致：默认范围随会话返回，前端据此自动带出 org/period/system_ids。
        default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
      } satisfies SessionOut,
    };
  }
  if (method === 'POST' && path === '/v1/assistant/suggest') {
    const payload = (body ?? {}) as Partial<SuggestIn>;
    if (payload.kind && payload.kind !== 'next_action') {
      return problem(422, 'E-PARSE-FAILED', `不支持的助手建议类型：${payload.kind}`);
    }
    const row = TICKETS.find((item) => item.ticket_id === payload.ticket_id);
    if (!row) return problem(422, 'E-DATA-MISSING', `工单 ${payload.ticket_id ?? '空'} 不存在`);
    return { status: 200, body: assistantSuggestion(row) };
  }
  if (method === 'GET' && path === '/v1/recon/batches') {
    const limit = Math.min(Number(q.limit ?? 20) || 20, 50);
    const start = Number(q.cursor ?? 0) || 0;
    const items = BATCH_SUMMARIES.slice(start, start + limit);
    const next = start + items.length < BATCH_SUMMARIES.length ? String(start + items.length) : null;
    return {
      status: 200,
      body: { items, next_cursor: next } satisfies PageReconBatchSummaryOut,
    };
  }
  if (method === 'GET' && path === '/v1/tickets') {
    let rows = [...TICKETS];
    if (q.state) rows = rows.filter((row) => row.state === q.state);
    if (q.scene_code) rows = rows.filter((row) => row.scene_code === q.scene_code);
    if (q.ticket_no) rows = rows.filter((row) => row.ticket_no === q.ticket_no);
    const items = rows.slice(0, Number(q.limit ?? 20));
    return { status: 200, body: { items, next_cursor: null } satisfies PageTicketOut };
  }
  if (method === 'GET' && parts[0] === 'tickets' && parts.length === 2) {
    try {
      return { status: 200, body: detail(findTicket(parts[1])) };
    } catch {
      return problem(422, 'E-DATA-MISSING', `工单 ${parts[1]} 不存在`);
    }
  }
  if (method === 'POST' && parts[0] === 'tickets' && parts[2] === 'actions') {
    try {
      return action(parts[1], parts[3], (body ?? {}) as Record<string, unknown>);
    } catch {
      return problem(422, 'E-DATA-MISSING', `工单 ${parts[1]} 不存在`);
    }
  }
  if (method === 'GET' && path === '/v1/approvals/pending') {
    return { status: 200, body: { items: pendingTasks(), next_cursor: null } satisfies PageApprovalTaskOut };
  }
  if (method === 'POST' && parts[0] === 'approvals' && (parts[2] === 'approve' || parts[2] === 'reject')) {
    try {
      return decide(parts[1], parts[2] === 'approve' ? 'approve' : 'reject', (body ?? {}) as Record<string, unknown>);
    } catch {
      return problem(422, 'E-DATA-MISSING', `工单 ${parts[1]} 不存在`);
    }
  }
  if (method === 'POST' && path === '/v1/recon/runs') {
    const payload = (body ?? {}) as Partial<ReconRunIn>;
    const batch = batchOut();
    batch.suppliers = payload.suppliers ?? batch.suppliers;
    return { status: 201, body: batch };
  }
  if (method === 'GET' && parts[0] === 'recon' && parts[1] === 'runs' && parts.length === 3) {
    return { status: 200, body: batchOut(parts[2]) };
  }
  if (method === 'GET' && parts[0] === 'recon' && parts[1] === 'runs' && parts[3] === 'diffs' && parts.length === 4) {
    // 旧批次没有差异投影：返回空页，而不是借用当前批次的差异。
    let rows = parts[2] === BATCH_OLD ? [] : [...DIFFS];
    if (q.diff_type) rows = rows.filter((row) => row.type === q.diff_type);
    if (q.needs_manual === 'true') rows = rows.filter((row) => row.needs_manual);
    return { status: 200, body: { items: rows, next_cursor: null } satisfies PageReconDiffOut };
  }
  if (method === 'GET' && parts[0] === 'recon' && parts[1] === 'runs' && parts[3] === 'diffs' && parts.length === 5) {
    try {
      return { status: 200, body: diffDetail(parts[4]) };
    } catch {
      return problem(422, 'E-DATA-MISSING', `差异 ${parts[4]} 不存在`);
    }
  }
  if (method === 'GET' && parts[0] === 'metering' && parts[1] === 'batches' && parts[3] === 'statement') {
    if (q.format === 'csv') {
      const rows = metering().lines ?? [];
      const lines = ['line_no,side,status,auto_released,needs_manual,confidence,ticket_no,our_ref,supplier_ref,diff_types'];
      rows.forEach((row) => {
        lines.push([row.line_no, row.side, row.status, row.auto_released, row.needs_manual, row.confidence, row.ticket_no ?? '', row.our_ref ?? '', row.supplier_ref ?? '', (row.diff_types ?? []).join('|')].join(','));
      });
      return {
        status: 200,
        text: '\ufeff' + lines.join('\n') + '\n',
        headers: {
          'content-type': 'text/csv; charset=utf-8',
          'content-disposition': 'attachment; filename="metering-mock.csv"',
          'x-content-sha256': 'sha256:mock-metering',
        },
      };
    }
    return { status: 200, body: metering() };
  }
  if (method === 'GET' && parts[0] === 'audit' && parts[1] === 'tickets' && parts[3] === 'images') {
    const target = TICKETS.find((row) => row.ticket_id === parts[2]);
    const rows = target ? imagesFor(target) : [];
    return { status: 200, body: { items: rows, next_cursor: null } satisfies PageAuditImageOut };
  }
  if (method === 'GET' && parts[0] === 'audit' && parts[1] === 'tickets' && parts[3] === 'chain') {
    const target = TICKETS.find((row) => row.ticket_id === parts[2]);
    const images = target ? imagesFor(target) : [];
    const audit = chain();
    audit.ticket_id = parts[2];
    audit.image_count = images.length;
    audit.checked = Math.max(1, images.length);
    audit.head_hash = images[images.length - 1]?.hash ?? 'GENESIS';
    return { status: 200, body: audit };
  }
  if (method === 'GET' && parts[0] === 'audit' && parts[1] === 'export') {
    const target = TICKETS.find((row) => row.ticket_id === String(q.ticket_id ?? ''));
    const rows = target ? imagesFor(target) : [];
    const lines = ['image_id,step_id,system,object_type,object_id,prev_hash,hash,diff_missing,created_at'];
    rows.forEach((row) => {
      lines.push([row.image_id, row.step_id, row.system, row.object_type, row.object_id, row.prev_hash, row.hash, row.diff_missing, row.created_at].join(','));
    });
    return {
      status: 200,
      text: lines.join('\n') + '\n',
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': 'attachment; filename="audit-mock.csv"',
        'x-content-sha256': 'sha256:mock-export',
      },
    };
  }
  return problem(404, 'E-DATA-MISSING', `mock 未实现 ${method} ${path}`);
}
