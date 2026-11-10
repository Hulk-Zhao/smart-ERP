/**
 * 本文件由 `node scripts/gen-api-types.mjs` 从 C16 冻结契约生成，请勿手改。
 * 源文件：contracts/api/product_api.openapi.v1.json
 * 契约 sha256：506b163d83b67e36a4601246cbe87d95a3bb63f2e41b46484a2f2d0f7264adf4
 * 契约版本：1.0.0 · C16
 *
 * 契约变更流程：后端重新生成契约 → 执行 `npm run gen:api` → 评审类型差异。
 */

export const CONTRACT_SHA256 = "506b163d83b67e36a4601246cbe87d95a3bb63f2e41b46484a2f2d0f7264adf4";
export const CONTRACT_VERSION = "1.0.0";
export const CONTRACT_PATH_COUNT = 17;

/** 写接口（POST）路径；前端统一补 Idempotency-Key。 */
export const WRITE_PATHS = ["/v1/approvals/{ticket_id}/approve","/v1/approvals/{ticket_id}/reject","/v1/recon/runs","/v1/tickets/{ticket_id}/actions/approve","/v1/tickets/{ticket_id}/actions/cancel","/v1/tickets/{ticket_id}/actions/compensate","/v1/tickets/{ticket_id}/actions/reject"] as const;

/**
 * 工单动作入参；``comment`` 同时承担审批意见 / 终止原因（FR-107）。
 */
export interface ActionIn {
  comment?: string;
  confirmed_manual?: boolean;
  fail_code?: ("F1" | "F2" | "F3" | "F4" | "F5" | "F6" | "F7") | null;
  role?: ("reviewer" | "approver" | "cfo") | null;
  scope: ScopeIn;
}

export interface ActionOut {
  action: "approve" | "reject" | "cancel" | "compensate";
  executed_steps?: TicketStepOut[];
  manual_tasks?: string[];
  missing_roles?: string[];
  required_roles?: string[];
  state: string;
  ticket_id: string;
  version: number;
}

export interface ApprovalDecisionIn {
  comment?: string;
  role: "reviewer" | "approver" | "cfo";
  scope: ScopeIn;
}

export interface ApprovalDecisionOut {
  decided_at: string;
  decision: "approved" | "rejected";
  missing_roles: string[];
  required_roles: string[];
  role: string;
  satisfied: boolean;
  ticket_id: string;
  ticket_state: string;
}

export interface ApprovalOut {
  approver_id: string;
  comment: string;
  decided_at: string;
  decision: "approved" | "rejected";
  role: string;
}

export interface ApprovalTaskOut {
  amount: string;
  business_key: string;
  created_at: string;
  initiator_id: string;
  missing_roles: string[];
  required_roles: string[];
  scene_code: string;
  state: string;
  ticket_id: string;
  ticket_no: string;
  touches_fund: boolean;
  write_level: string;
}

export interface AuditChainOut {
  broken_at?: number | null;
  checked: number;
  detail?: string;
  head_hash: string;
  image_count: number;
  ok: boolean;
  tenant_id: string;
  ticket_id: string;
}

export interface AuditImageOut {
  after_ref?: string | null;
  before_ref?: string | null;
  created_at: string;
  diff_missing: boolean;
  field_diff?: Record<string, unknown>[];
  hash: string;
  image_id: string;
  object_id: string;
  object_type: string;
  prev_hash: string;
  step_id: string;
  system: string;
  ticket_id: string;
}

/**
 * 行/差异的计费桶：与计量表四类计数共用同一分类函数，禁止第二处重算。
 */
export type BillingBucket = "billable" | "auto_released" | "dispute_hold" | "other_unbillable";

export type DiffTypeCode = "unit_price" | "qty" | "scrap" | "uom" | "tax" | "missing_doc" | "duplicate" | "rounding_tolerance";

export interface DiscrepancyDraftOut {
  amount: MoneyOut;
  confidence: string;
  diff_type: string;
  draft_no: string;
  needs_manual: boolean;
  owner: string;
  period: string;
  refs: string[];
  status: string;
  suggestion: string;
  supplier_code: string;
}

export interface EvidenceOut {
  coord?: Record<string, unknown> | null;
  doc_no: string;
  field: string;
  system: string;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface ItemMasterIn {
  base_unit?: string | null;
  code: string;
  name?: string | null;
  spec?: string | null;
}

export interface MasterDataIn {
  items?: ItemMasterIn[];
  suppliers?: SupplierMasterIn[];
  unit_conversions?: UnitConversionIn[];
}

/**
 * 计量所需但当前数据模型提供不了的字段：如实报缺，不用默认值冒充。
 */
export interface MeteringDataGapOut {
  action: string;
  field: string;
  owner_role: string;
  reason: string;
}

export interface MeteringExcludedOut {
  customer_reason?: number | null;
  dispute_hold: number;
  misrelease?: number | null;
  other_reasons?: Record<string, unknown>;
  other_unbillable: number;
  total: number;
}

/**
 * 按明细行的程序性事实；不判断「结论是否正确」。
 */
export interface MeteringLineOut {
  after_ref?: string | null;
  auto_released: boolean;
  before_ref?: string | null;
  billing_bucket: BillingBucket;
  confidence: string;
  diff_types?: string[];
  evidence?: EvidenceOut[];
  excluded_reason?: string;
  human_modified?: boolean | null;
  line_no: number;
  needs_manual: boolean;
  our_ref?: string | null;
  owner?: string | null;
  side: "supplier" | "our";
  source_refs?: string[];
  status: string;
  supplier_code?: string | null;
  supplier_ref?: string | null;
  ticket_no?: string | null;
}

/**
 * 产品文档 07 §2.2.1 计量表字段；金额一律十进制文本。
 */
export interface MeteringStatementOut {
  auto_released_lines: number;
  batch_id: string;
  batch_status: string;
  billable_basis: string;
  billable_lines: number;
  calculation?: string[];
  cap_amount?: string | null;
  cap_exceeded: boolean;
  cap_remaining?: string | null;
  computed_at: string;
  conclusive: boolean;
  confirmed_misrelease_lines?: number | null;
  cumulative_amount: string;
  currency: string;
  customer_reason_lines?: number | null;
  data_gaps?: MeteringDataGapOut[];
  excluded_lines: MeteringExcludedOut;
  expected_lines: number;
  human_reviewed_lines?: number | null;
  line_accounting: string;
  line_count: number;
  lines?: MeteringLineOut[];
  period: string;
  period_amount: string;
  prior_cumulative_amount: string;
  supplier_scope: string[];
  unit_price: string;
  unit_price_source: string;
}

export interface MissingItemOut {
  code: string;
  detail: string;
  kind: string;
  source_ref: string;
}

export interface MoneyOut {
  amount: string;
  currency: string;
  tax_included: boolean;
}

export interface PageApprovalTaskOut {
  items: ApprovalTaskOut[];
  next_cursor?: string | null;
}

export interface PageAuditImageOut {
  items: AuditImageOut[];
  next_cursor?: string | null;
}

export interface PageReconDiffOut {
  items: ReconDiffOut[];
  next_cursor?: string | null;
}

export interface PageTicketOut {
  items: TicketOut[];
  next_cursor?: string | null;
}

export interface ReconBatchOut {
  batch_id: string;
  conclusive: boolean;
  created_at: string;
  created_by: string;
  diff_count?: number;
  metrics?: ReconMetricsOut | null;
  missing_items?: MissingItemOut[];
  org_id: string;
  period: string;
  status: string;
  status_reason: string;
  suppliers: string[];
  system_ids: string[];
}

/**
 * 本次对账的取数结果；缺省时按 ``sources.statement_files`` 找已登记数据集。
 */
export interface ReconDatasetIn {
  master_data?: MasterDataIn;
  our_lines?: ReconLineIn[];
  supplier_lines?: ReconLineIn[];
  tolerance?: ToleranceIn | null;
}

export interface ReconDiffDetailOut {
  amount: MoneyOut;
  batch_id: string;
  billing_bucket: BillingBucket;
  confidence: string;
  diff_id: string;
  draft?: DiscrepancyDraftOut | null;
  evidence?: EvidenceOut[];
  needs_manual: boolean;
  owner: string;
  ref: string;
  related_lines?: ReconLineResultOut[];
  suggestion: string;
  supplier_code: string;
  type: string;
  unattributed?: UnattributedOut | null;
}

export interface ReconDiffOut {
  amount: MoneyOut;
  billing_bucket: BillingBucket;
  confidence: string;
  diff_id: string;
  evidence?: EvidenceOut[];
  needs_manual: boolean;
  owner: string;
  ref: string;
  suggestion: string;
  supplier_code: string;
  type: string;
}

/**
 * 对账输入行；字段与 ``apps.recon.loader._build_line`` 一一对应。
 */
export interface ReconLineIn {
  amount?: number | string | null;
  coord?: Record<string, unknown> | null;
  currency?: string;
  doc_no: string;
  item_code?: string | null;
  item_name?: string | null;
  line_no?: number;
  period?: string | null;
  qty?: number | string | null;
  scrap_qty?: number | string | null;
  supplier_code: string;
  system: string;
  tax_included?: boolean;
  tax_rate?: number | string | null;
  unit?: string | null;
  unit_price?: number | string | null;
}

export interface ReconLineResultOut {
  amount?: MoneyOut | null;
  confidence: string;
  item?: string | null;
  match_type: string;
  needs_manual: boolean;
  note?: string;
  our_ref?: string | null;
  qty_mine?: string | null;
  qty_supplier?: string | null;
  status: string;
  supplier_ref?: string | null;
}

export interface ReconMetricsOut {
  ambiguous: number;
  auto_disposal_allowed: boolean;
  candidate_only: number;
  diff_amount: string;
  diff_lines: number;
  diff_rate: string;
  matched: number;
  matched_rate: string;
  supplier_lines: number;
  suspected_anomaly: boolean;
  unmatched: number;
}

export interface ReconRunIn {
  dataset?: ReconDatasetIn | null;
  rerun_of?: string | null;
  rerun_reason?: string | null;
  scope: ScopeIn;
  sources?: ReconSourcesIn;
  suppliers: string[];
}

/**
 * 取数来源声明（与 AG-1 入参 C7 的 ``sources`` 对齐）。
 */
export interface ReconSourcesIn {
  erp?: boolean;
  mes?: boolean;
  statement_files?: string[];
  wms?: boolean;
}

/**
 * G-05：三个字段缺一不可，只允许在授权范围内收窄。
 */
export interface ScopeIn {
  org_id: string;
  period: string;
  system_ids: string[];
}

export interface SupplierMasterIn {
  aliases?: string[];
  code: string;
  name?: string | null;
  tax_no?: string | null;
}

export interface TicketDetailOut {
  amount: string;
  approvals: ApprovalOut[];
  audit_image_count: number;
  business_key: string;
  created_at: string;
  initiator_id: string;
  missing_roles: string[];
  org_id: string;
  period: string;
  required_roles: string[];
  scene_code: string;
  state: TicketStateCode;
  steps: TicketStepOut[];
  systems: string[];
  ticket_id: string;
  ticket_no: string;
  touches_fund: boolean;
  updated_at: string;
  version: number;
  write_level: string;
}

export interface TicketOut {
  amount: string;
  business_key: string;
  created_at: string;
  initiator_id: string;
  org_id: string;
  period: string;
  scene_code: string;
  state: TicketStateCode;
  systems: string[];
  ticket_id: string;
  ticket_no: string;
  touches_fund: boolean;
  updated_at: string;
  version: number;
  write_level: string;
}

export type TicketStateCode = "计划中" | "待确认" | "待审批" | "执行中" | "校验中" | "已完成" | "补偿中" | "已回滚" | "需人工处置" | "已锁定" | "已终止" | "已驳回";

export interface TicketStepOut {
  action_key: string;
  executed_at?: string | null;
  idem_key: string;
  level: string;
  object_id: string;
  object_type: string;
  state: string;
  step_id: string;
  step_no: number;
  system: string;
}

export interface ToleranceIn {
  amount?: number | string | null;
  price?: number | string | null;
  qty?: number | string | null;
}

export interface UnattributedOut {
  amount_diff: MoneyOut;
  evidence?: EvidenceOut[];
  reason: string;
  ref: string;
}

export interface UnitConversionIn {
  factor: number | string;
  from_unit: string;
  item: string;
  to_unit: string;
}

export interface ValidationError {
  ctx?: Record<string, never>;
  input?: unknown;
  loc: (string | number)[];
  msg: string;
  type: string;
}

/**
 * 契约路径表：键为 `METHOD /path`，值给出 query / body / response 类型。
 * 页面与 client 只从这里取返回类型，避免手写类型与契约脱节。
 */
export interface ApiOperations {
  "GET /v1/approvals/pending": {
    query: {
    scene_code?: string | null;
    cursor?: string | null;
    limit?: number | null;
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: PageApprovalTaskOut;
  };
  "POST /v1/approvals/{ticket_id}/approve": {
    query: never;
    body: ApprovalDecisionIn;
    response: ApprovalDecisionOut;
  };
  "POST /v1/approvals/{ticket_id}/reject": {
    query: never;
    body: ApprovalDecisionIn;
    response: ApprovalDecisionOut;
  };
  "GET /v1/audit/export": {
    query: {
    ticket_id: string;
    system?: string | null;
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: unknown;
  };
  "GET /v1/audit/tickets/{ticket_id}/chain": {
    query: {
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: AuditChainOut;
  };
  "GET /v1/audit/tickets/{ticket_id}/images": {
    query: {
    system?: string | null;
    step_id?: string | null;
    cursor?: string | null;
    limit?: number | null;
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: PageAuditImageOut;
  };
  "GET /v1/metering/batches/{batch_id}/statement": {
    query: {
    format?: "json" | "csv";
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: MeteringStatementOut;
  };
  "POST /v1/recon/runs": {
    query: never;
    body: ReconRunIn;
    response: ReconBatchOut;
  };
  "GET /v1/recon/runs/{batch_id}": {
    query: {
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: ReconBatchOut;
  };
  "GET /v1/recon/runs/{batch_id}/diffs": {
    query: {
    diff_type?: DiffTypeCode | null;
    needs_manual?: boolean | null;
    supplier_code?: string | null;
    billing_bucket?: BillingBucket | null;
    cursor?: string | null;
    limit?: number | null;
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: PageReconDiffOut;
  };
  "GET /v1/recon/runs/{batch_id}/diffs/{diff_id}": {
    query: {
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: ReconDiffDetailOut;
  };
  "GET /v1/tickets": {
    query: {
    state?: string | null;
    scene_code?: string | null;
    business_key?: string | null;
    ticket_no?: string | null;
    initiator_id?: string | null;
    cursor?: string | null;
    limit?: number | null;
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: PageTicketOut;
  };
  "GET /v1/tickets/{ticket_id}": {
    query: {
    org_id: string;
    period: string;
    system_ids: string[];
  };
    body: never;
    response: TicketDetailOut;
  };
  "POST /v1/tickets/{ticket_id}/actions/approve": {
    query: never;
    body: ActionIn;
    response: ActionOut;
  };
  "POST /v1/tickets/{ticket_id}/actions/cancel": {
    query: never;
    body: ActionIn;
    response: ActionOut;
  };
  "POST /v1/tickets/{ticket_id}/actions/compensate": {
    query: never;
    body: ActionIn;
    response: ActionOut;
  };
  "POST /v1/tickets/{ticket_id}/actions/reject": {
    query: never;
    body: ActionIn;
    response: ActionOut;
  };
}
