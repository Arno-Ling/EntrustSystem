-- =============================================================================
-- Migration 012: 材料供应方式前移到询价阶段
-- =============================================================================
-- 将 material_sourcing 决策从 outsource_orders（中标后）前移到 outsource_requests（询价前）
-- 新增多行材料询价支持 + 加工方逐项材料单价 + 材料方逐行报价
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. outsource_requests：加 material_sourcing 字段
-- -----------------------------------------------------------------------------
ALTER TABLE outsource_requests
    ADD COLUMN IF NOT EXISTS material_sourcing VARCHAR(16);

COMMENT ON COLUMN outsource_requests.material_sourcing IS
  '材料供应方式：internal=我方统一供料 / processor=加工方自采；NULL=未决定';


-- -----------------------------------------------------------------------------
-- 2. material_inquiry_lines：多行材料询价明细
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_inquiry_lines (
    id              SERIAL PRIMARY KEY,
    inquiry_id      INTEGER NOT NULL REFERENCES material_inquiries(id) ON DELETE CASCADE,
    material_code   VARCHAR(64) NOT NULL,
    material_name   VARCHAR(128),
    spec            VARCHAR(128),
    qty             DECIMAL(14,2) NOT NULL,
    unit            VARCHAR(16) DEFAULT 'kg',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mil_inquiry ON material_inquiry_lines (inquiry_id);

COMMENT ON TABLE material_inquiry_lines IS '材料询价行（一张询价单包含多种材料）';


-- -----------------------------------------------------------------------------
-- 3. material_inquiries：加 outsource_request_id，outsource_order_id 改为可空
-- -----------------------------------------------------------------------------
ALTER TABLE material_inquiries
    ADD COLUMN IF NOT EXISTS outsource_request_id INTEGER REFERENCES outsource_requests(id);

-- outsource_order_id 改为可空（询价阶段还没有 order）
ALTER TABLE material_inquiries
    ALTER COLUMN outsource_order_id DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mi_request ON material_inquiries (outsource_request_id);


-- -----------------------------------------------------------------------------
-- 4. outsource_quotation_lines：加 material_unit_price（加工方自采时填）
-- -----------------------------------------------------------------------------
ALTER TABLE outsource_quotation_lines
    ADD COLUMN IF NOT EXISTS material_unit_price DECIMAL(14,2);

COMMENT ON COLUMN outsource_quotation_lines.material_unit_price IS
  '材料单价（仅 material_sourcing=processor 时由加工方填写）';


-- -----------------------------------------------------------------------------
-- 5. material_quotations：加 inquiry_line_id，改唯一约束支持逐行报价
-- -----------------------------------------------------------------------------
ALTER TABLE material_quotations
    ADD COLUMN IF NOT EXISTS inquiry_line_id INTEGER REFERENCES material_inquiry_lines(id);

-- 删除旧的 invitation_id 唯一约束（一个邀请只能一个报价 → 一个邀请+行 一个报价）
ALTER TABLE material_quotations
    DROP CONSTRAINT IF EXISTS material_quotations_invitation_id_key;

-- 新唯一约束：同一邀请 + 同一询价行 只能有一条报价
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uk_mq_inv_line'
    ) THEN
        ALTER TABLE material_quotations
            ADD CONSTRAINT uk_mq_inv_line UNIQUE (invitation_id, inquiry_line_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mq_line ON material_quotations (inquiry_line_id);


-- -----------------------------------------------------------------------------
-- 完成校验
-- -----------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='outsource_requests' AND column_name='material_sourcing') AS req_col,
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='public' AND table_name='material_inquiry_lines') AS new_table,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='material_inquiries' AND column_name='outsource_request_id') AS mi_col,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='outsource_quotation_lines' AND column_name='material_unit_price') AS oql_col,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='material_quotations' AND column_name='inquiry_line_id') AS mq_col;
-- 期望: 全部 = 1
