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
  ApprovalTaskOut,
  AuditChainOut,
  AuditImageOut,
  MeteringStatementOut,
  PageApprovalTaskOut,
  PageAuditImageOut,
  PageReconDiffOut,
  PageTicketOut,
  ReconBatchOut,
  ReconDiffDetailOut,
  ReconDiffOut,
  ReconLineResultOut,
  ReconRunIn,
  TicketDetailOut,
  TicketOut,
  TicketStepOut,
} from './types';

const TENANT = '11111111-1111-1111-1111-111111111111';
const INITIATOR = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const REVIEWER = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

const T_W3 = 'd0000000-0000-4000-8000-000000000001';
const T_NORMAL = 'd0000000-0000-4000-8000-000000000002';
const T_DUAL = 'd0000000-0000-4000-8000-000000000003';
const T_EXECUTING = 'd0000000-0000-4000-8000-000000000004';
const T_RECON = 'd0000000-0000-4000-8000-000000000005';
const BATCH = 'e0000000-0000-4000-8000-000000000001';

function ticket(partial: Partial<TicketOut> & Pick<TicketOut, 'ticket_id' | 'ticket_no' | 'state'>): TicketOut {
  return {
    scene_code: 'ag1_reconcile',
    version: 1,
    initiator_id: INITIATOR,
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

const TICKETS: TicketOut[] = [
  ticket({
    ticket_id: T_W3,
    ticket_no: 'T-1002',
    state: '待审批',
    write_level: 'W3',
    amount: '100.0000',
    business_key: 'BK-W3',
  }),
  ticket({
    ticket_id: T_NORMAL,
    ticket_no: 'T-1001',
    state: '待审批',
    amount: '1200.0000',
    business_key: 'BK-NORMAL',
  }),
  ticket({
    ticket_id: T_DUAL,
    ticket_no: 'T-1003',
    state: '待审批',
    write_level: 'W2',
    amount: '200000.0000',
    business_key: 'BK-DUAL',
  }),
  ticket({
    ticket_id: T_EXECUTING,
    ticket_no: 'T-1007',
    state: '执行中',
    amount: '8600.0000',
    business_key: 'BK-EXEC',
  }),
  ticket({
    ticket_id: T_RECON,
    ticket_no: 'T-1008',
    state: '已完成',
    amount: '0.0000',
    business_key: 'BK-RECON',
  }),
];

const STEPS: Record<string, TicketStepOut[]> = {
  [T_NORMAL]: [
    {
      step_no: 1,
      step_id: 'f0000000-0000-4000-8000-000000000001',
      action_key: 'u8.trade.save_draft',
      system: 'u8',
      object_type: 'draft',
      object_id: 'D-1001',
      state: 'succeeded',
      level: 'W1',
      idem_key: 'idem-t1001',
      executed_at: '2026-11-10T09:20:00Z',
    },
  ],
  [T_EXECUTING]: [
    {
      step_no: 1,
      step_id: 'f0000000-0000-4000-8000-000000000002',
      action_key: 'u8.trade.save_draft',
      system: 'u8',
      object_type: 'draft',
      object_id: 'D-1007',
      state: 'succeeded',
      level: 'W1',
      idem_key: 'idem-t1007',
      executed_at: '2026-11-10T09:10:00Z',
    },
    {
      step_no: 2,
      step_id: 'f0000000-0000-4000-8000-000000000003',
      action_key: 'u8.trade.submit',
      system: 'u8',
      object_type: 'draft',
      object_id: 'D-1007',
      state: 'failed',
      level: 'W1',
      idem_key: 'idem-t1007-2',
      executed_at: '2026-11-10T09:12:00Z',
    },
  ],
};

const IMAGES: Record<string, AuditImageOut[]> = {
  [T_NORMAL]: [
    {
      image_id: 'a0000000-0000-4000-8000-000000000001',
      ticket_id: T_NORMAL,
      step_id: 'f0000000-0000-4000-8000-000000000001',
      system: 'u8',
      object_type: 'draft',
      object_id: 'D-1001',
      before_ref: 'mem://before/1001',
      after_ref: 'mem://after/1001',
      field_diff: [
        { field: 'qty', label: '数量', before: 100, after: 112, kind: 'qty', unit: '件' },
        { field: 'amount', label: '金额', before: '1000.0000', after: '1200.0000', kind: 'amount' },
      ],
      diff_missing: false,
      prev_hash: 'GENESIS',
      hash: 'sha256:mock-1001',
      created_at: '2026-11-10T09:20:00Z',
    },
  ],
  [T_EXECUTING]: [
    {
      image_id: 'a0000000-0000-4000-8000-000000000002',
      ticket_id: T_EXECUTING,
      step_id: 'f0000000-0000-4000-8000-000000000002',
      system: 'u8',
      object_type: 'draft',
      object_id: 'D-1007',
      before_ref: 'mem://before/1007',
      after_ref: 'mem://after/1007',
      field_diff: [{ field: 'amount', label: '金额', before: '8000.0000', after: '8600.0000', kind: 'amount' }],
      diff_missing: false,
      prev_hash: 'sha256:mock-1001',
      hash: 'sha256:mock-1007',
      created_at: '2026-11-10T09:10:00Z',
    },
  ],
};

function detail(row: TicketOut): TicketDetailOut {
  const required = row.write_level === 'W3' ? [] : row.write_level === 'W2' ? ['reviewer', 'approver'] : ['reviewer'];
  return {
    ...row,
    steps: STEPS[row.ticket_id] ?? [],
    approvals: row.ticket_id === T_DUAL
      ? [
          {
            role: 'reviewer',
            approver_id: REVIEWER,
            decision: 'approved',
            comment: '复核通过（该意见不得展示给其他审批人）',
            decided_at: '2026-11-10T09:25:00Z',
          },
        ]
      : [],
    required_roles: required,
    missing_roles: row.ticket_id === T_DUAL ? ['approver'] : required,
    audit_image_count: (IMAGES[row.ticket_id] ?? []).length,
  };
}

const METRICS: ReconBatchOut['metrics'] = {
  supplier_lines: 192,
  matched: 180,
  matched_rate: '0.9375',
  ambiguous: 3,
  candidate_only: 2,
  unmatched: 7,
  diff_lines: 9,
  diff_rate: '0.046875',
  diff_amount: '3280.5000',
  suspected_anomaly: false,
  auto_disposal_allowed: true,
};

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
    amount: { amount: '1200.0000', currency: 'CNY', tax_included: true },
    owner: 'supplier',
    suggestion: '单价 12.5 与对账单 13.2 不一致，核对价格协议/调价生效日期',
    confidence: '0.9',
    needs_manual: true,
    ref: 'statement:ST-202610-001#2',
    supplier_code: 'S-001',
    evidence: [
      { system: 'statement', doc_no: 'ST-202610-001', field: 'unit_price', coord: { page: 2, bbox: [300, 212, 380, 226] } },
    ],
  },
  {
    diff_id: 'd2',
    type: 'rounding_tolerance',
    billing_bucket: 'other_unbillable',
    amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
    owner: 'system',
    suggestion: '数量/单价一致，金额差 0.03 元在舍入容差 0.05 元内，按容差放行',
    confidence: '0.95',
    needs_manual: false,
    ref: 'statement:ST-202610-002#1',
    supplier_code: 'S-002',
    evidence: [],
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
      required_roles: required,
      missing_roles: row.ticket_id === T_DUAL ? ['approver'] : required,
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
      required_roles: [],
      missing_roles: [],
      executed_steps: STEPS[row.ticket_id] ?? [],
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
    if (!body.fail_code) return problem(422, 'E-DATA-MISSING', '触发补偿必须声明失败分类（fail_code）');
    if (!['执行中', '补偿中'].includes(row.state)) {
      return problem(409, 'E-STATE-ILLEGAL', '只有执行中/补偿中可触发补偿');
    }
    row.state = '已回滚';
    row.version += 1;
    return {
      status: 200,
      body: {
        ticket_id: row.ticket_id,
        action: 'compensate',
        state: row.state,
        version: row.version,
        executed_steps: [],
        manual_tasks: [],
      },
    };
  }
  return problem(422, 'E-PARSE-FAILED', '未知动作');
}

function decide(ticketId: string, decision: 'approve' | 'reject', body: Record<string, unknown>) {
  const row = findTicket(ticketId);
  if (row.state !== '待审批') return problem(409, 'E-STATE-ILLEGAL', '不在待审批');
  if (!body.role) return problem(409, 'E-POLICY-APPROVAL', '审批角色必填');
  if (row.ticket_id === T_DUAL && body.role !== 'approver') {
    return problem(409, 'E-POLICY-APPROVAL', '审批未满足：还需 approver（财务/业务负责人）审批', {
      missing_roles: ['approver'],
      required_roles: ['reviewer', 'approver'],
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
    required_roles: row.ticket_id === T_DUAL ? ['reviewer', 'approver'] : ['reviewer'],
    missing_roles: row.ticket_id === T_DUAL && body.role !== 'approver' ? ['approver'] : [],
    ticket_state: row.state,
  };
  return { status: 200, body: out };
}

function batchOut(): ReconBatchOut {
  return {
    batch_id: BATCH,
    status: 'reconciled',
    conclusive: true,
    status_reason: '全部供应商对账单已核完，差异均在可处置范围',
    org_id: 'ORG-1',
    period: '2026-10',
    system_ids: ['u8', 'statement'],
    suppliers: ['S-001', 'S-002'],
    metrics: METRICS,
    missing_items: [],
    diff_count: DIFFS.length,
    created_by: INITIATOR,
    created_at: '2026-11-10T10:00:00Z',
  };
}

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
    billable_basis: '自动放行 − 已确认错放行 − 客户原因',
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
    line_accounting: '有效行数只计程序性事实，不判断结论是否正确',
    calculation: ['自动放行 59 行', '已确认错放行 0 行（无数据源，按 0 扣减）', '客户原因 0 行（无数据源，按 0 扣减）', '59 − 0 − 0 = 59 行 × 0.8000 CNY = 47.2000 CNY'],
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
    line_count: 2,
    lines: [
      {
        line_no: 1,
        side: 'supplier',
        status: '已对账',
        billing_bucket: 'billable',
        auto_released: true,
        needs_manual: false,
        confidence: '0.98',
        ticket_no: 'T-1001',
        supplier_code: 'S-001',
        owner: 'system',
        our_ref: 'u8:PO-202610-001#1',
        supplier_ref: 'statement:ST-202610-001#1',
        before_ref: null,
        after_ref: 'audit:image-1',
        human_modified: null,
        diff_types: [],
        source_refs: ['statement:ST-202610-001#1'],
        evidence: [],
        excluded_reason: '',
      },
      {
        line_no: 2,
        side: 'supplier',
        status: '差异挂起',
        billing_bucket: 'dispute_hold',
        auto_released: false,
        needs_manual: true,
        confidence: '0.72',
        ticket_no: 'T-1002',
        supplier_code: 'S-001',
        owner: 'supplier',
        our_ref: 'u8:PO-202610-002#1',
        supplier_ref: 'statement:ST-202610-002#1',
        before_ref: null,
        after_ref: null,
        human_modified: null,
        diff_types: ['rounding_tolerance'],
        source_refs: ['statement:ST-202610-002#1'],
        evidence: [],
        excluded_reason: '差异未确认，挂起不计费',
      },
    ],
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

  if (method === 'GET' && path === '/v1/tickets') {
    let rows = [...TICKETS];
    if (q.state) rows = rows.filter((row) => row.state === q.state);
    if (q.scene_code) rows = rows.filter((row) => row.scene_code === q.scene_code);
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
    return { status: 200, body: batchOut() };
  }
  if (method === 'GET' && parts[0] === 'recon' && parts[2] === 'diffs' && parts.length === 4) {
    let rows = [...DIFFS];
    if (q.diff_type) rows = rows.filter((row) => row.type === q.diff_type);
    if (q.needs_manual === 'true') rows = rows.filter((row) => row.needs_manual);
    return { status: 200, body: { items: rows, next_cursor: null } satisfies PageReconDiffOut };
  }
  if (method === 'GET' && parts[0] === 'recon' && parts[2] === 'diffs' && parts.length === 5) {
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
  if (method === 'GET' && parts[0] === 'audit' && parts[2] === 'images') {
    const rows = IMAGES[parts[1]] ?? [];
    return { status: 200, body: { items: rows, next_cursor: null } satisfies PageAuditImageOut };
  }
  if (method === 'GET' && parts[0] === 'audit' && parts[2] === 'chain') {
    const audit = chain();
    audit.ticket_id = parts[1];
    return { status: 200, body: audit };
  }
  if (method === 'GET' && parts[0] === 'audit' && parts[1] === 'export') {
    const rows = IMAGES[String(q.ticket_id ?? '')] ?? [];
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
