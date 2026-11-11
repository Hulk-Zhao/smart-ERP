/**
 * 界面层类型与展示元数据。
 *
 * 契约实体类型一律从 `./generated/contract.ts` 重导出，禁止在此手写与 C16 脱节的字段。
 * 这里只放 UI 需要的标签、颜色、状态机展示顺序（源自 contracts/ticket/state_machine.v1.json）。
 */

import type { DiffTypeCode } from './generated/contract';

export type {
  ActionIn,
  ActionOut,
  ApprovalDecisionIn,
  ApprovalDecisionOut,
  ApprovalOut,
  ApprovalTaskOut,
  AuditChainOut,
  AuditImageOut,
  BillingBucket,
  DiffTypeCode,
  DiscrepancyDraftOut,
  EvidenceOut,
  MissingItemOut,
  MeteringDataGapOut,
  MeteringExcludedOut,
  MeteringLineOut,
  MeteringStatementOut,
  MoneyOut,
  PageApprovalTaskOut,
  PageAuditImageOut,
  PageReconBatchSummaryOut,
  PageReconDiffOut,
  PageTicketOut,
  ReconBatchOut,
  ReconBatchSummaryOut,
  ReconDatasetIn,
  ReconDiffDetailOut,
  ReconDiffOut,
  ReconLineResultOut,
  ReconMetricsOut,
  ReconRunIn,
  ReconStatementOut,
  ScopeIn,
  SessionOut,
  SuggestIn,
  SuggestOut,
  TicketDetailOut,
  TicketOut,
  TicketStepOut,
  UnattributedOut,
  ApiOperations,
} from './generated/contract';

export { CONTRACT_PATH_COUNT, CONTRACT_SHA256, CONTRACT_VERSION } from './generated/contract';

/** 审计影像/工单步骤的字段差异是契约里的宽松对象，界面按已知键渲染、未知键原样展示。 */
export type FieldDiffEntry = Record<string, unknown>;

export type TicketState =
  | '计划中'
  | '待确认'
  | '待审批'
  | '执行中'
  | '校验中'
  | '已完成'
  | '补偿中'
  | '已回滚'
  | '需人工处置'
  | '已锁定'
  | '已终止'
  | '已驳回';

/** 与 contracts/ticket/state_machine.v1.json 的 states 顺序一致。 */
export const TICKET_STATES: TicketState[] = [
  '计划中',
  '待确认',
  '待审批',
  '执行中',
  '校验中',
  '已完成',
  '补偿中',
  '已回滚',
  '需人工处置',
  '已锁定',
  '已终止',
  '已驳回',
];

export const TERMINAL_STATES: TicketState[] = [
  '已完成',
  '已回滚',
  '需人工处置',
  '已锁定',
  '已终止',
  '已驳回',
];

/** 与 state_machine.v1.json 的 transitions 一致；用于展示「下一步可达状态」。 */
export const TRANSITIONS: Record<TicketState, TicketState[]> = {
  计划中: ['待确认', '待审批', '已终止'],
  待确认: ['待审批', '已终止'],
  待审批: ['执行中', '已驳回', '已终止'],
  执行中: ['校验中', '已锁定', '补偿中'],
  校验中: ['已完成', '补偿中'],
  补偿中: ['已回滚', '需人工处置'],
  已完成: [],
  已回滚: [],
  需人工处置: [],
  已锁定: [],
  已终止: [],
  已驳回: [],
};

export const STEP_STATE_LABELS: Record<string, string> = {
  intent: '意图',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  skipped: '跳过',
  compensating: '补偿中',
  compensated: '已补偿',
  unresolved: '待人工',
  rolled_back: '已回滚',
};

export const ROLE_LABELS: Record<string, string> = {
  reviewer: '复核人',
  approver: '审批人',
  cfo: '财务负责人',
};

export const WRITE_LEVEL_LABELS: Record<string, string> = {
  W0: 'W0 · 只读/内部',
  W1: 'W1 · 低风险写入',
  W2: 'W2 · 需审批写入',
  W3: 'W3 · 禁止自动执行',
};

export interface DiffTypeMeta {
  // 直接从契约生成物取联合类型：新增/改名会在编译期报错，避免标签比枚举多一类或少一类。
  code: DiffTypeCode;
  label: string;
  tone: string;
}

/**
 * 差异类型码：与后端 DiffType 枚举的 8 类一一对应，`rounding_tolerance` 为后端稳定映射值。
 * 契约里 diff_type 是 string，具体值仍以 `ReconDiffOut['type']` 为准。
 */
export const DIFF_TYPES: DiffTypeMeta[] = [
  { code: 'unit_price', label: '单价不一致', tone: 'bg-amber-50 text-amber-700 ring-amber-200' },
  { code: 'qty', label: '数量不符', tone: 'bg-sky-50 text-sky-700 ring-sky-200' },
  { code: 'scrap', label: '报废未扣', tone: 'bg-orange-50 text-orange-700 ring-orange-200' },
  { code: 'uom', label: '单位换算', tone: 'bg-violet-50 text-violet-700 ring-violet-200' },
  { code: 'tax', label: '税率口径', tone: 'bg-teal-50 text-teal-700 ring-teal-200' },
  { code: 'missing_doc', label: '缺单', tone: 'bg-rose-50 text-rose-700 ring-rose-200' },
  { code: 'duplicate', label: '重复计费', tone: 'bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-200' },
  { code: 'rounding_tolerance', label: '金额舍入容差', tone: 'bg-lime-50 text-lime-700 ring-lime-200' },
];

export function diffTypeMeta(code: string | null | undefined): { code: string; label: string; tone: string } {
  return (
    DIFF_TYPES.find((item) => item.code === code) ?? {
      code: code ?? 'unknown',
      label: code ?? '未知差异',
      tone: 'bg-slate-100 text-slate-600 ring-slate-200',
    }
  );
}
