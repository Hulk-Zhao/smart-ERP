import type {
  ApprovalTaskOut,
  AuditChainOut,
  AuditImageOut,
  MeteringStatementOut,
  ReconBatchOut,
  ReconDiffDetailOut,
  ReconDiffOut,
  TicketDetailOut,
  TicketOut,
} from '../../../web/src/api/generated/contract';

export const W3_TICKET_ID = 'd0000000-0000-4000-8000-000000000001';
export const DUAL_TICKET_ID = 'd0000000-0000-4000-8000-000000000002';
export const BATCH_ID = 'e0000000-0000-4000-8000-000000000001';
export const DIFF_ID = 'f0000000-0000-4000-8000-000000000001';
export const OTHER_APPROVER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
export const INITIATOR_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

const BASE_TICKET = {
  scene_code: 'ag1_reconcile',
  version: 3,
  initiator_id: INITIATOR_ID,
  org_id: 'ORG-1',
  period: '2026-10',
  systems: ['u8', 'statement'],
  amount: '1200.0000',
  touches_fund: false,
  created_at: '2026-11-10T09:00:00Z',
  updated_at: '2026-11-10T09:30:00Z',
};

export function ticketOut(overrides: Partial<TicketOut> = {}): TicketOut {
  return {
    ...BASE_TICKET,
    ticket_id: W3_TICKET_ID,
    ticket_no: 'T-W3',
    business_key: 'BK-W3',
    state: '待审批',
    write_level: 'W3',
    ...overrides,
  };
}

export function ticketDetail(overrides: Partial<TicketDetailOut> = {}): TicketDetailOut {
  return {
    ...ticketOut(overrides),
    steps: [
      {
        step_no: 1,
        step_id: 'f0000000-0000-4000-8000-000000000011',
        action_key: 'u8.trade.save_draft',
        system: 'u8',
        object_type: 'draft',
        object_id: 'D-9001',
        state: 'succeeded',
        level: 'W1',
        idem_key: 'idem-w3-1',
        executed_at: '2026-11-10T09:10:00Z',
      },
    ],
    approvals: [],
    required_roles: [],
    missing_roles: [],
    audit_image_count: 1,
    ...overrides,
  };
}

export function auditImage(overrides: Partial<AuditImageOut> = {}): AuditImageOut {
  return {
    image_id: 'a0000000-0000-4000-8000-000000000001',
    ticket_id: W3_TICKET_ID,
    step_id: 'f0000000-0000-4000-8000-000000000011',
    system: 'u8',
    object_type: 'draft',
    object_id: 'D-9001',
    before_ref: 'u8://PO-202610-001#qty@1:72,138,210,152',
    after_ref: 'u8://PO-202610-001#qty@1:72,138,210,152?after',
    field_diff: [
      { field: 'qty', label: '数量', before: 100, after: 112, kind: 'qty', unit: '件' },
      { field: 'amount', label: '金额', before: '1000.0000', after: '1200.0000', kind: 'amount' },
    ],
    diff_missing: false,
    prev_hash: 'GENESIS',
    hash: 'sha256:test-image-1',
    created_at: '2026-11-10T09:10:00Z',
    ...overrides,
  };
}

export function auditChain(overrides: Partial<AuditChainOut> = {}): AuditChainOut {
  return {
    ticket_id: W3_TICKET_ID,
    tenant_id: '11111111-1111-1111-1111-111111111111',
    ok: true,
    checked: 1,
    broken_at: null,
    detail: '哈希链完整',
    image_count: 1,
    head_hash: 'sha256:test-image-1',
    ...overrides,
  };
}

export function approvalTask(overrides: Partial<ApprovalTaskOut> = {}): ApprovalTaskOut {
  return {
    ticket_id: DUAL_TICKET_ID,
    ticket_no: 'T-DUAL',
    scene_code: 'ag1_reconcile',
    business_key: 'BK-DUAL',
    state: '待审批',
    write_level: 'W2',
    amount: '200000.0000',
    touches_fund: true,
    initiator_id: INITIATOR_ID,
    required_roles: ['reviewer', 'approver'],
    missing_roles: ['approver'],
    created_at: '2026-11-10T09:00:00Z',
    ...overrides,
  };
}

export function reconBatch(overrides: Partial<ReconBatchOut> = {}): ReconBatchOut {
  return {
    batch_id: BATCH_ID,
    status: 'reconciled',
    conclusive: true,
    status_reason: '全部供应商已核完',
    org_id: 'ORG-1',
    period: '2026-10',
    system_ids: ['u8', 'statement'],
    suppliers: ['S-001'],
    metrics: {
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
    },
    missing_items: [],
    diff_count: 1,
    created_by: INITIATOR_ID,
    created_at: '2026-11-10T10:00:00Z',
    ...overrides,
  };
}

export function reconDiff(overrides: Partial<ReconDiffOut> = {}): ReconDiffOut {
  return {
    diff_id: DIFF_ID,
    type: 'rounding_tolerance',
    amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
    owner: 'system',
    suggestion: '数量/单价一致，金额差 0.03 元在舍入容差 0.05 元内，按容差放行',
    confidence: '0.95',
    needs_manual: false,
    ref: 'statement:ST-202610-001#1',
    supplier_code: 'S-001',
    evidence: [
      {
        system: 'u8',
        doc_no: 'PO-202610-001',
        field: 'amount',
        coord: { page: 2, bbox: [10, 20, 30, 40] },
      },
    ],
    ...overrides,
  };
}

export function reconDiffDetail(overrides: Partial<ReconDiffDetailOut> = {}): ReconDiffDetailOut {
  return {
    ...reconDiff(overrides),
    batch_id: BATCH_ID,
    related_lines: [
      {
        item: 'A-001',
        qty_mine: '100',
        qty_supplier: '100',
        amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
        match_type: 'single',
        confidence: '0.95',
        status: 'diff',
        our_ref: 'u8:PO-202610-001#1',
        supplier_ref: 'statement:ST-202610-001#1',
        needs_manual: false,
        note: '金额舍入差 0.03 元',
      },
    ],
    draft: {
      draft_no: 'DT-TEST-001',
      supplier_code: 'S-001',
      period: '2026-10',
      diff_type: 'rounding_tolerance',
      amount: { amount: '0.0300', currency: 'CNY', tax_included: true },
      owner: 'system',
      suggestion: '按舍入容差放行',
      confidence: '0.95',
      needs_manual: false,
      refs: ['statement:ST-202610-001#1'],
      status: '草稿',
    },
    unattributed: null,
    ...overrides,
  };
}

export function meteringStatement(overrides: Partial<MeteringStatementOut> = {}): MeteringStatementOut {
  return {
    batch_id: BATCH_ID,
    batch_status: '已完成',
    conclusive: true,
    period: '2026-10',
    supplier_scope: ['S-001'],
    expected_lines: 64,
    auto_released_lines: 59,
    billable_lines: 59,
    billable_basis: '自动放行 − 已确认错放行 − 客户原因',
    excluded_lines: {
      total: 5,
      dispute_hold: 3,
      other_unbillable: 2,
      misrelease: null,
      customer_reason: null,
      other_reasons: { 重复单据: 2 },
    },
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
    calculation: [
      '自动放行 59 行',
      '已确认错放行 0 行（无数据源，按 0 扣减）',
      '59 − 0 − 0 = 59 行 × 0.8000 CNY = 47.2000 CNY',
    ],
    data_gaps: [
      { field: 'cap_amount', reason: '合同封顶金额未进入数据模型，无法计算封顶余额', owner_role: '采购/合同法务', action: '提供合同封顶金额与计算口径' },
      { field: 'confirmed_misrelease_lines', reason: '错放行确认结果当前无数据源', owner_role: '对账运营', action: '在批次结案后回填错放行确认结果' },
      { field: 'customer_reason_lines', reason: '客户原因分类当前无数据源', owner_role: '客户成功/商务', action: '提供客户原因分类与凭证' },
    ],
    computed_at: '2026-11-10T12:00:00Z',
    line_count: 2,
    lines: [
      {
        line_no: 1,
        side: 'supplier',
        status: '已对账',
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
    ...overrides,
  };
}
