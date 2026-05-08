-- =============================================================================
-- Migration 011: 材料供应方式分叉
-- =============================================================================
-- 分支 A (internal_source):  我方发询价单给材料方，材料方报价，我方选中标
-- 分支 B (processor_source): 加工方自己找材料方，上传采购凭证存证
--
-- 新增：
--   1. outsource_orders 加 material_sourcing / sourcing_decided_* 列
--   2. material_inquiries              我方给材料方的询价单
--   3. material_inquiry_invitations    询价邀请
--   4. material_quotations             材料方报价
--   5. processor_material_proofs       加工方自采凭证
--   6. material_purchase_orders 加 sourced_from_inquiry_id / outsource_order_id
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. outsource_orders：加材料供应方式
-- -----------------------------------------------------------------------------
ALTER TABLE outsource_orders
    ADD COLUMN IF NOT EXISTS material_sourcing            VARCHAR(16),
    ADD COLUMN IF NOT EXISTS material_sourcing_decided_by INTEGER REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS material_sourcing_decided_at TIMESTAMP;

COMMENT ON COLUMN outsource_orders.material_sourcing IS
  '材料供应方式：internal=我方找材料方发 PO / processor=加工方自采';
COMMENT ON COLUMN outsource_orders.material_sourcing_decided_by IS '决策人 user_id';
COMMENT ON COLUMN outsource_orders.material_sourcing_decided_at IS '决策时间';


-- -----------------------------------------------------------------------------
-- 2. material_inquiries —— 我方给材料方的询价单
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_inquiries (
    id                    SERIAL PRIMARY KEY,
    inquiry_no            VARCHAR(64) NOT NULL UNIQUE,
    outsource_order_id    INTEGER NOT NULL REFERENCES outsource_orders(id) ON DELETE CASCADE,
    project_id            INTEGER NOT NULL REFERENCES projects(id),
    title                 VARCHAR(255) NOT NULL,

    material_code         VARCHAR(64)  NOT NULL,
    material_name         VARCHAR(128),
    spec                  VARCHAR(128),
    qty                   DECIMAL(14,2) NOT NULL,
    unit                  VARCHAR(16) DEFAULT 'kg',

    required_date         DATE,
    status                VARCHAR(32) NOT NULL DEFAULT 'draft',
        -- draft / inviting / comparing / pending_award / awarded / cancelled
    closed_at             TIMESTAMP,
    approval_task_id      BIGINT,
    winning_quotation_id  INTEGER,
    created_by            INTEGER REFERENCES users(id),
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mi_order    ON material_inquiries (outsource_order_id);
CREATE INDEX IF NOT EXISTS idx_mi_project  ON material_inquiries (project_id);
CREATE INDEX IF NOT EXISTS idx_mi_status   ON material_inquiries (status);

DROP TRIGGER IF EXISTS tr_mi_updated_at ON material_inquiries;
CREATE TRIGGER tr_mi_updated_at BEFORE UPDATE ON material_inquiries
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

COMMENT ON TABLE  material_inquiries IS '材料询价单（我方发给材料方，仅 internal_source 模式用）';
COMMENT ON COLUMN material_inquiries.inquiry_no IS '单号：MI-<proj>-<seq>';
COMMENT ON COLUMN material_inquiries.status IS 'draft 草稿 / inviting 群发中 / comparing 待中标 / pending_award / awarded / cancelled';


-- -----------------------------------------------------------------------------
-- 3. material_inquiry_invitations —— 询价邀请
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_inquiry_invitations (
    id                SERIAL PRIMARY KEY,
    inquiry_id        INTEGER NOT NULL REFERENCES material_inquiries(id) ON DELETE CASCADE,
    supplier_id       INTEGER NOT NULL REFERENCES suppliers(id),
    tenant_id         INTEGER REFERENCES tenants(id),
    invitation_status VARCHAR(32) NOT NULL DEFAULT 'sent',
        -- sent / quoted / no_response / cancelled
    sent_at           TIMESTAMP NOT NULL,
    quoted_at         TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_mi_inv_unique UNIQUE (inquiry_id, supplier_id)
);
CREATE INDEX IF NOT EXISTS idx_mi_inv_inquiry ON material_inquiry_invitations (inquiry_id);
CREATE INDEX IF NOT EXISTS idx_mi_inv_tenant  ON material_inquiry_invitations (tenant_id, invitation_status);

COMMENT ON TABLE material_inquiry_invitations IS '材料询价邀请（一询价单对多材料方）';


-- -----------------------------------------------------------------------------
-- 4. material_quotations —— 材料方报价
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_quotations (
    id               SERIAL PRIMARY KEY,
    invitation_id    INTEGER NOT NULL UNIQUE REFERENCES material_inquiry_invitations(id) ON DELETE CASCADE,
    unit_price       DECIMAL(14,2) NOT NULL,
    lead_time_days   INTEGER NOT NULL,
    note             TEXT,
    submitted_by     INTEGER REFERENCES users(id),
    submitted_at     TIMESTAMP NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mq_invitation ON material_quotations (invitation_id);

COMMENT ON TABLE material_quotations IS '材料方针对材料询价的报价（一邀请一报价）';


-- -----------------------------------------------------------------------------
-- 5. processor_material_proofs —— 加工方自采凭证（processor_source 模式）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processor_material_proofs (
    id                 SERIAL PRIMARY KEY,
    outsource_order_id INTEGER NOT NULL REFERENCES outsource_orders(id) ON DELETE CASCADE,
    proof_type         VARCHAR(32) NOT NULL,
        -- invoice 发票 / contract 合同 / photo 照片 / certificate 材质证书 / waybill 运单
    attachment_id      INTEGER REFERENCES attachments(id),
    supplier_name_text VARCHAR(255),
        -- 加工方自填的供应商名称（不强绑 suppliers 表）
    batch_no           VARCHAR(64),
    material_code      VARCHAR(64),
    spec               VARCHAR(128),
    qty                DECIMAL(14,2),
    unit               VARCHAR(16) DEFAULT 'kg',
    purchase_date      DATE,
    note               TEXT,
    uploaded_by        INTEGER REFERENCES users(id),
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pmp_order ON processor_material_proofs (outsource_order_id);

COMMENT ON TABLE processor_material_proofs IS '加工方自采原料凭证（processor_source 模式；用于异常追责时溯源）';
COMMENT ON COLUMN processor_material_proofs.proof_type IS 'invoice 发票 / contract 合同 / photo 物料照 / certificate 材质证书 / waybill 运单';


-- -----------------------------------------------------------------------------
-- 6. material_purchase_orders —— 加两个关联字段
-- -----------------------------------------------------------------------------
ALTER TABLE material_purchase_orders
    ADD COLUMN IF NOT EXISTS sourced_from_inquiry_id INTEGER REFERENCES material_inquiries(id),
    ADD COLUMN IF NOT EXISTS outsource_order_id      INTEGER REFERENCES outsource_orders(id);

CREATE INDEX IF NOT EXISTS idx_mpo_inquiry  ON material_purchase_orders (sourced_from_inquiry_id);
CREATE INDEX IF NOT EXISTS idx_mpo_oo       ON material_purchase_orders (outsource_order_id);

COMMENT ON COLUMN material_purchase_orders.sourced_from_inquiry_id IS
  '来源询价单（若由 material_inquiries 审批通过后自动生成）';
COMMENT ON COLUMN material_purchase_orders.outsource_order_id IS
  '关联的外协加工单（表示"这批料是给哪个加工单备的"）';


-- -----------------------------------------------------------------------------
-- 完成校验
-- -----------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='public'
       AND table_name IN ('material_inquiries','material_inquiry_invitations',
                          'material_quotations','processor_material_proofs')) AS new_tables,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='outsource_orders'
       AND column_name IN ('material_sourcing','material_sourcing_decided_by','material_sourcing_decided_at')) AS oo_cols,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='material_purchase_orders'
       AND column_name IN ('sourced_from_inquiry_id','outsource_order_id')) AS mpo_cols;
-- 期望: new_tables=4, oo_cols=3, mpo_cols=2
