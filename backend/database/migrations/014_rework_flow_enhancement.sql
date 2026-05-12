-- =============================================================================
-- Migration 014: 返工流程增强
-- =============================================================================
-- 1. rework_orders 加字段（确认/交货/质检/费用追踪）
-- 2. material_redelivery_requests 新表（材料方责任+我方供料时的补发请求）
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. rework_orders 扩展字段
-- -----------------------------------------------------------------------------
ALTER TABLE rework_orders
    ADD COLUMN IF NOT EXISTS confirmed_at           TIMESTAMP,
    ADD COLUMN IF NOT EXISTS confirmed_by           INTEGER REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS confirm_remark         TEXT,
    ADD COLUMN IF NOT EXISTS delivered_at           TIMESTAMP,
    ADD COLUMN IF NOT EXISTS deliver_photos         JSONB DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS deliver_remark         TEXT,
    ADD COLUMN IF NOT EXISTS inspected_at           TIMESTAMP,
    ADD COLUMN IF NOT EXISTS completed_at           TIMESTAMP,
    ADD COLUMN IF NOT EXISTS material_sourcing_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS cost_bearer            VARCHAR(32) DEFAULT 'processor',
    ADD COLUMN IF NOT EXISTS cost_amount            DECIMAL(14,2),
    ADD COLUMN IF NOT EXISTS cost_remark            TEXT,
    ADD COLUMN IF NOT EXISTS new_po_id              INTEGER REFERENCES material_purchase_orders(id);

COMMENT ON COLUMN rework_orders.confirmed_at IS '加工方确认时间';
COMMENT ON COLUMN rework_orders.confirmed_by IS '加工方确认人 user_id';
COMMENT ON COLUMN rework_orders.confirm_remark IS '加工方确认备注';
COMMENT ON COLUMN rework_orders.delivered_at IS '加工方交货时间';
COMMENT ON COLUMN rework_orders.deliver_photos IS '交货照片路径数组 JSON';
COMMENT ON COLUMN rework_orders.deliver_remark IS '交货备注';
COMMENT ON COLUMN rework_orders.inspected_at IS '质检开始时间';
COMMENT ON COLUMN rework_orders.completed_at IS '返工完成时间';
COMMENT ON COLUMN rework_orders.material_sourcing_mode IS '材料供应模式快照: internal/processor';
COMMENT ON COLUMN rework_orders.cost_bearer IS '费用承担方: processor/internal/shared';
COMMENT ON COLUMN rework_orders.cost_amount IS '费用金额';
COMMENT ON COLUMN rework_orders.cost_remark IS '费用备注';
COMMENT ON COLUMN rework_orders.new_po_id IS '返工补料单 ID（Branch A 自动创建）';


-- -----------------------------------------------------------------------------
-- 2. material_redelivery_requests（材料方补发请求）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_redelivery_requests (
    id                  SERIAL PRIMARY KEY,
    exception_id        INTEGER NOT NULL REFERENCES quality_exceptions(id),
    original_po_id      INTEGER REFERENCES material_purchase_orders(id),
    supplier_id         INTEGER REFERENCES suppliers(id),
    tenant_id           INTEGER REFERENCES tenants(id),
    project_id          INTEGER REFERENCES projects(id),
    material_code       VARCHAR(64),
    spec                VARCHAR(128),
    qty                 DECIMAL(14,2),
    unit                VARCHAR(16) DEFAULT 'kg',
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',
        -- pending / shipped / received / cancelled
    remark              TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    shipped_at          TIMESTAMP,
    received_at         TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mrr_exception ON material_redelivery_requests (exception_id);
CREATE INDEX IF NOT EXISTS idx_mrr_supplier ON material_redelivery_requests (supplier_id);

COMMENT ON TABLE material_redelivery_requests IS '材料方补发请求（材料方责任+我方供料时，要求材料方重新发货）';


-- -----------------------------------------------------------------------------
-- 完成校验
-- -----------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='rework_orders' AND column_name IN (
       'confirmed_at','confirmed_by','confirm_remark','delivered_at',
       'deliver_photos','deliver_remark','inspected_at','completed_at',
       'material_sourcing_mode','cost_bearer','cost_amount','cost_remark','new_po_id'
     )) AS rw_cols,
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='public' AND table_name='material_redelivery_requests') AS new_table;
-- 期望: rw_cols=13, new_table=1
