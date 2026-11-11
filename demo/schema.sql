-- smart-ERP demo 数据集业务库（schema: demo）
--
-- 边界：
--   1. 本 schema 只放「演示用业务数据」，不承载产品 API 的运行时投影；
--      工单/对账/审计/计价投影仍写入 erp.*（见 demo/load.py 的 --with-erp）。
--   2. 所有表都带 scenario 列，一套场景可以单独清空/重灌，互不影响。
--   3. 金额一律 numeric(18,4)、日期 date、时间 timestamptz；字符集由数据库统一 UTF-8。
--   4. 幂等：CREATE TABLE IF NOT EXISTS；数据清空由 load.py 按 scenario 执行。
--
-- 口径：订单行金额 = 数量 × 单价 × (1-折扣)，两位小数；订单头 = 行合计；
--       税金 = 头金额 × 税率；价税合计 = 金额 + 税金。load.py 会逐条复核这些等式。

CREATE SCHEMA IF NOT EXISTS demo;

-- ---------------------------------------------------------------- 场景与组织
CREATE TABLE IF NOT EXISTS demo.scenario (
    scenario        text PRIMARY KEY,
    display_name    text NOT NULL,
    industry        text NOT NULL,
    story           text NOT NULL,
    base_currency   char(3) NOT NULL,
    timezone        text NOT NULL DEFAULT 'Asia/Shanghai',
    generated_at    timestamptz NOT NULL,
    seed            bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS demo.organization (
    scenario        text NOT NULL,
    org_code        text NOT NULL,
    org_name        text NOT NULL,
    legal_entity    text NOT NULL,
    country         char(2) NOT NULL,
    base_currency   char(3) NOT NULL,
    timezone        text NOT NULL DEFAULT 'Asia/Shanghai',
    PRIMARY KEY (scenario, org_code)
);

CREATE TABLE IF NOT EXISTS demo.warehouse (
    scenario        text NOT NULL,
    wh_code         text NOT NULL,
    org_code        text NOT NULL,
    wh_name         text NOT NULL,
    wh_type         text NOT NULL,
    province        text NOT NULL,
    city            text NOT NULL,
    address         text NOT NULL,
    PRIMARY KEY (scenario, wh_code)
);

CREATE TABLE IF NOT EXISTS demo.currency (
    scenario        text NOT NULL,
    currency        char(3) NOT NULL,
    currency_name   text NOT NULL,
    rate_to_cny     numeric(18,6) NOT NULL,
    rate_date       date NOT NULL,
    PRIMARY KEY (scenario, currency, rate_date)
);

CREATE TABLE IF NOT EXISTS demo.tax_rate (
    scenario        text NOT NULL,
    tax_code        text NOT NULL,
    tax_name        text NOT NULL,
    country         char(2) NOT NULL,
    rate            numeric(9,6) NOT NULL,
    effective_from  date NOT NULL,
    PRIMARY KEY (scenario, tax_code)
);

-- ---------------------------------------------------------------- 主数据
CREATE TABLE IF NOT EXISTS demo.customer (
    scenario        text NOT NULL,
    customer_code   text NOT NULL,
    customer_name   text NOT NULL,
    customer_group  text NOT NULL,
    parent_code     text,
    tier            text NOT NULL,
    channel         text NOT NULL,
    org_code        text NOT NULL,
    currency        char(3) NOT NULL,
    payment_terms   integer NOT NULL,
    credit_limit    numeric(18,4) NOT NULL,
    credit_used     numeric(18,4) NOT NULL,
    tax_id          text NOT NULL,
    province        text NOT NULL,
    city            text NOT NULL,
    address         text NOT NULL,
    sales_rep       text NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, customer_code)
);

CREATE TABLE IF NOT EXISTS demo.supplier (
    scenario        text NOT NULL,
    supplier_code   text NOT NULL,
    supplier_name   text NOT NULL,
    category        text NOT NULL,
    grade           text NOT NULL,
    org_code        text NOT NULL,
    currency        char(3) NOT NULL,
    payment_terms   integer NOT NULL,
    country         char(2) NOT NULL,
    contact_name    text NOT NULL,
    phone           text NOT NULL,
    tax_id          text NOT NULL,
    province        text NOT NULL,
    city            text NOT NULL,
    address         text NOT NULL,
    on_time_rate    numeric(5,2) NOT NULL,
    quality_rate    numeric(5,2) NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, supplier_code)
);

CREATE TABLE IF NOT EXISTS demo.item (
    scenario        text NOT NULL,
    item_code       text NOT NULL,
    item_name       text NOT NULL,
    item_type       text NOT NULL,
    category        text NOT NULL,
    uom             text NOT NULL,
    spec            text NOT NULL,
    std_cost        numeric(18,4) NOT NULL,
    list_price      numeric(18,4) NOT NULL,
    tax_code        text NOT NULL,
    shelf_life_days integer,
    abc_class       text NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, item_code)
);

CREATE TABLE IF NOT EXISTS demo.bom_line (
    scenario        text NOT NULL,
    bom_code        text NOT NULL,
    parent_item     text NOT NULL,
    child_item      text NOT NULL,
    level           integer NOT NULL,
    qty_per         numeric(18,6) NOT NULL,
    scrap_rate      numeric(9,6) NOT NULL,
    uom             text NOT NULL,
    effective_from  date NOT NULL,
    PRIMARY KEY (scenario, bom_code, parent_item, child_item)
);

-- ---------------------------------------------------------------- 销售
CREATE TABLE IF NOT EXISTS demo.sales_order (
    scenario        text NOT NULL,
    so_no           text NOT NULL,
    customer_code   text NOT NULL,
    org_code        text NOT NULL,
    order_date      date NOT NULL,
    promised_date   date NOT NULL,
    currency        char(3) NOT NULL,
    payment_terms   integer NOT NULL,
    subtotal        numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    total_amount    numeric(18,4) NOT NULL,
    status          text NOT NULL,
    sales_rep       text NOT NULL,
    PRIMARY KEY (scenario, so_no)
);

CREATE TABLE IF NOT EXISTS demo.sales_order_line (
    scenario        text NOT NULL,
    so_no           text NOT NULL,
    line_no         integer NOT NULL,
    item_code       text NOT NULL,
    qty             numeric(18,4) NOT NULL,
    uom             text NOT NULL,
    unit_price      numeric(18,4) NOT NULL,
    discount_rate   numeric(9,6) NOT NULL,
    tax_code        text NOT NULL,
    amount          numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    warehouse_code  text NOT NULL,
    PRIMARY KEY (scenario, so_no, line_no)
);

CREATE TABLE IF NOT EXISTS demo.sales_delivery (
    scenario        text NOT NULL,
    delivery_no     text NOT NULL,
    so_no           text NOT NULL,
    customer_code   text NOT NULL,
    warehouse_code  text NOT NULL,
    delivery_date   date NOT NULL,
    carrier         text NOT NULL,
    tracking_no     text NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, delivery_no)
);

CREATE TABLE IF NOT EXISTS demo.sales_delivery_line (
    scenario        text NOT NULL,
    delivery_no     text NOT NULL,
    line_no         integer NOT NULL,
    so_no           text NOT NULL,
    so_line_no      integer NOT NULL,
    item_code       text NOT NULL,
    qty             numeric(18,4) NOT NULL,
    lot_no          text NOT NULL,
    PRIMARY KEY (scenario, delivery_no, line_no)
);

CREATE TABLE IF NOT EXISTS demo.sales_invoice (
    scenario        text NOT NULL,
    invoice_no      text NOT NULL,
    customer_code   text NOT NULL,
    delivery_no     text NOT NULL,
    org_code        text NOT NULL,
    invoice_date    date NOT NULL,
    due_date        date NOT NULL,
    currency        char(3) NOT NULL,
    fx_rate         numeric(18,6) NOT NULL,
    subtotal        numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    total_amount    numeric(18,4) NOT NULL,
    total_cny       numeric(18,4) NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, invoice_no)
);

CREATE TABLE IF NOT EXISTS demo.sales_invoice_line (
    scenario        text NOT NULL,
    invoice_no      text NOT NULL,
    line_no         integer NOT NULL,
    delivery_no     text NOT NULL,
    delivery_line_no integer NOT NULL,
    item_code       text NOT NULL,
    qty             numeric(18,4) NOT NULL,
    unit_price      numeric(18,4) NOT NULL,
    tax_code        text NOT NULL,
    amount          numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    PRIMARY KEY (scenario, invoice_no, line_no)
);

-- ---------------------------------------------------------------- 采购
CREATE TABLE IF NOT EXISTS demo.purchase_order (
    scenario        text NOT NULL,
    po_no           text NOT NULL,
    supplier_code   text NOT NULL,
    org_code        text NOT NULL,
    order_date      date NOT NULL,
    expected_date   date NOT NULL,
    currency        char(3) NOT NULL,
    payment_terms   integer NOT NULL,
    subtotal        numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    total_amount    numeric(18,4) NOT NULL,
    status          text NOT NULL,
    buyer           text NOT NULL,
    PRIMARY KEY (scenario, po_no)
);

CREATE TABLE IF NOT EXISTS demo.purchase_order_line (
    scenario        text NOT NULL,
    po_no           text NOT NULL,
    line_no         integer NOT NULL,
    item_code       text NOT NULL,
    qty             numeric(18,4) NOT NULL,
    uom             text NOT NULL,
    unit_price      numeric(18,4) NOT NULL,
    tax_code        text NOT NULL,
    amount          numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    PRIMARY KEY (scenario, po_no, line_no)
);

CREATE TABLE IF NOT EXISTS demo.goods_receipt (
    scenario        text NOT NULL,
    receipt_no      text NOT NULL,
    po_no           text NOT NULL,
    supplier_code   text NOT NULL,
    warehouse_code  text NOT NULL,
    receipt_date    date NOT NULL,
    inspector       text NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, receipt_no)
);

CREATE TABLE IF NOT EXISTS demo.goods_receipt_line (
    scenario        text NOT NULL,
    receipt_no      text NOT NULL,
    line_no         integer NOT NULL,
    po_no           text NOT NULL,
    po_line_no      integer NOT NULL,
    item_code       text NOT NULL,
    qty_received    numeric(18,4) NOT NULL,
    qty_rejected    numeric(18,4) NOT NULL,
    lot_no          text NOT NULL,
    PRIMARY KEY (scenario, receipt_no, line_no)
);

CREATE TABLE IF NOT EXISTS demo.purchase_invoice (
    scenario        text NOT NULL,
    invoice_no      text NOT NULL,
    supplier_code   text NOT NULL,
    receipt_no      text NOT NULL,
    org_code        text NOT NULL,
    invoice_date    date NOT NULL,
    due_date        date NOT NULL,
    currency        char(3) NOT NULL,
    fx_rate         numeric(18,6) NOT NULL,
    subtotal        numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    total_amount    numeric(18,4) NOT NULL,
    total_cny       numeric(18,4) NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, invoice_no)
);

CREATE TABLE IF NOT EXISTS demo.purchase_invoice_line (
    scenario        text NOT NULL,
    invoice_no      text NOT NULL,
    line_no         integer NOT NULL,
    receipt_no      text NOT NULL,
    receipt_line_no integer NOT NULL,
    item_code       text NOT NULL,
    qty             numeric(18,4) NOT NULL,
    unit_price      numeric(18,4) NOT NULL,
    tax_code        text NOT NULL,
    amount          numeric(18,4) NOT NULL,
    tax_amount      numeric(18,4) NOT NULL,
    PRIMARY KEY (scenario, invoice_no, line_no)
);

-- ---------------------------------------------------------------- 库存 / 生产
CREATE TABLE IF NOT EXISTS demo.inventory_lot (
    scenario        text NOT NULL,
    lot_no          text NOT NULL,
    item_code       text NOT NULL,
    warehouse_code  text NOT NULL,
    org_code        text NOT NULL,
    receipt_no      text NOT NULL,
    qty_received    numeric(18,4) NOT NULL,
    qty_on_hand     numeric(18,4) NOT NULL,
    unit_cost       numeric(18,4) NOT NULL,
    production_date date NOT NULL,
    expiry_date     date,
    age_days        integer NOT NULL,
    quality_status  text NOT NULL,
    PRIMARY KEY (scenario, lot_no)
);

CREATE TABLE IF NOT EXISTS demo.production_order (
    scenario        text NOT NULL,
    mo_no           text NOT NULL,
    org_code        text NOT NULL,
    item_code       text NOT NULL,
    warehouse_code  text NOT NULL,
    planned_qty     numeric(18,4) NOT NULL,
    completed_qty   numeric(18,4) NOT NULL,
    start_date      date NOT NULL,
    due_date        date NOT NULL,
    status          text NOT NULL,
    production_line text NOT NULL,
    shift           text NOT NULL,
    PRIMARY KEY (scenario, mo_no)
);

CREATE TABLE IF NOT EXISTS demo.production_material_issue (
    scenario        text NOT NULL,
    mo_no           text NOT NULL,
    line_no         integer NOT NULL,
    item_code       text NOT NULL,
    required_qty    numeric(18,4) NOT NULL,
    issued_qty      numeric(18,4) NOT NULL,
    lot_no          text NOT NULL,
    issue_date      date NOT NULL,
    PRIMARY KEY (scenario, mo_no, line_no)
);

-- ---------------------------------------------------------------- 总账
CREATE TABLE IF NOT EXISTS demo.gl_entry (
    scenario        text NOT NULL,
    voucher_no      text NOT NULL,
    line_no         integer NOT NULL,
    posting_date    date NOT NULL,
    account_code    text NOT NULL,
    account_name    text NOT NULL,
    doc_type        text NOT NULL,
    doc_no          text NOT NULL,
    currency        char(3) NOT NULL,
    debit           numeric(18,4) NOT NULL,
    credit          numeric(18,4) NOT NULL,
    memo            text NOT NULL,
    PRIMARY KEY (scenario, voucher_no, line_no)
);

-- ---------------------------------------------------------------- 对账案例（驱动 erp.recon_* 投影）
CREATE TABLE IF NOT EXISTS demo.recon_case (
    scenario        text NOT NULL,
    case_id         text NOT NULL,
    batch_code      text NOT NULL,
    period          text NOT NULL,
    supplier_code   text NOT NULL,
    supplier_name   text NOT NULL,
    line_no         integer NOT NULL,
    our_ref         text NOT NULL,
    supplier_ref    text NOT NULL,
    item_code       text NOT NULL,
    qty_mine        numeric(18,4),
    qty_supplier    numeric(18,4),
    our_amount      numeric(18,4) NOT NULL,
    supplier_amount numeric(18,4) NOT NULL,
    diff_amount     numeric(18,4) NOT NULL,
    currency        char(3) NOT NULL,
    diff_type       text NOT NULL,
    owner           text NOT NULL,
    line_status     text NOT NULL,
    needs_manual    boolean NOT NULL,
    confidence      numeric(5,4) NOT NULL,
    note            text NOT NULL,
    PRIMARY KEY (scenario, case_id)
);

-- ---------------------------------------------------------------- 工单案例（驱动 erp.ticket_* 投影）
CREATE TABLE IF NOT EXISTS demo.ticket_case (
    scenario        text NOT NULL,
    ticket_no       text NOT NULL,
    scene_code      text NOT NULL,
    ticket_state    text NOT NULL,
    write_level     text NOT NULL,
    amount          numeric(18,4) NOT NULL,
    touches_fund    boolean NOT NULL,
    org_id          text NOT NULL,
    period          text NOT NULL,
    systems         text NOT NULL,
    initiator_name  text NOT NULL,
    approver_name   text NOT NULL,
    business_key    text NOT NULL,
    subject         text NOT NULL,
    story           text NOT NULL,
    created_date    date NOT NULL,
    PRIMARY KEY (scenario, ticket_no)
);

-- ---------------------------------------------------------------- 快消：促销 / 返利
CREATE TABLE IF NOT EXISTS demo.promotion (
    scenario        text NOT NULL,
    promotion_code  text NOT NULL,
    promotion_name  text NOT NULL,
    channel         text NOT NULL,
    promo_type      text NOT NULL,
    start_date      date NOT NULL,
    end_date        date NOT NULL,
    budget_amount   numeric(18,4) NOT NULL,
    actual_amount   numeric(18,4) NOT NULL,
    status          text NOT NULL,
    owner           text NOT NULL,
    PRIMARY KEY (scenario, promotion_code)
);

CREATE TABLE IF NOT EXISTS demo.rebate_accrual (
    scenario        text NOT NULL,
    accrual_no      text NOT NULL,
    customer_code   text NOT NULL,
    period          text NOT NULL,
    tier            text NOT NULL,
    rebate_rate     numeric(9,6) NOT NULL,
    sales_base      numeric(18,4) NOT NULL,
    accrued_amount  numeric(18,4) NOT NULL,
    paid_amount     numeric(18,4) NOT NULL,
    status          text NOT NULL,
    settlement_date date,
    PRIMARY KEY (scenario, accrual_no)
);

-- ---------------------------------------------------------------- 跨境：汇率 / 报关 / 关联结算
CREATE TABLE IF NOT EXISTS demo.fx_rate (
    scenario        text NOT NULL,
    rate_date       date NOT NULL,
    pair            text NOT NULL,
    rate            numeric(18,6) NOT NULL,
    source          text NOT NULL,
    PRIMARY KEY (scenario, rate_date, pair)
);

CREATE TABLE IF NOT EXISTS demo.customs_declaration (
    scenario        text NOT NULL,
    declaration_no  text NOT NULL,
    receipt_no      text NOT NULL,
    supplier_code   text NOT NULL,
    org_code        text NOT NULL,
    declaration_date date NOT NULL,
    port            text NOT NULL,
    hs_code         text NOT NULL,
    declared_value  numeric(18,4) NOT NULL,
    currency        char(3) NOT NULL,
    duty_rate       numeric(9,6) NOT NULL,
    duty_amount     numeric(18,4) NOT NULL,
    vat_amount      numeric(18,4) NOT NULL,
    broker          text NOT NULL,
    status          text NOT NULL,
    PRIMARY KEY (scenario, declaration_no)
);

CREATE TABLE IF NOT EXISTS demo.intercompany_settlement (
    scenario        text NOT NULL,
    settlement_no   text NOT NULL,
    from_org        text NOT NULL,
    to_org          text NOT NULL,
    period          text NOT NULL,
    currency        char(3) NOT NULL,
    amount           numeric(18,4) NOT NULL,
    fx_rate         numeric(18,6) NOT NULL,
    amount_cny      numeric(18,4) NOT NULL,
    settlement_date date NOT NULL,
    status          text NOT NULL,
    note            text NOT NULL,
    PRIMARY KEY (scenario, settlement_no)
);

-- ---------------------------------------------------------------- 加载台账
CREATE TABLE IF NOT EXISTS demo.load_log (
    seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scenario     text NOT NULL,
    dataset_sha256 text NOT NULL,
    row_counts   jsonb NOT NULL,
    erp_counts   jsonb NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    loaded_by    text NOT NULL
);

CREATE INDEX IF NOT EXISTS demo_sales_order_date_idx ON demo.sales_order (scenario, order_date);
CREATE INDEX IF NOT EXISTS demo_purchase_order_date_idx ON demo.purchase_order (scenario, order_date);
CREATE INDEX IF NOT EXISTS demo_inventory_lot_age_idx ON demo.inventory_lot (scenario, age_days);
CREATE INDEX IF NOT EXISTS demo_recon_case_batch_idx ON demo.recon_case (scenario, batch_code);
CREATE INDEX IF NOT EXISTS demo_gl_entry_doc_idx ON demo.gl_entry (scenario, doc_no);
