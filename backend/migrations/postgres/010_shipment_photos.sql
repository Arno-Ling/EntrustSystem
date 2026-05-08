-- =============================================================================
-- Migration 010: 物流链拍照取证（3 个关卡）
-- =============================================================================
-- 关卡 1：材料方发货 → material_shipments.photo_paths（已存在，无需改）
-- 关卡 2：加工方接单收到物料 → outsource_orders.receive_photos
-- 关卡 3：加工方加工完成交货 → outsource_orders.complete_photos
-- =============================================================================

ALTER TABLE outsource_orders
    ADD COLUMN IF NOT EXISTS receive_photos  TEXT[],
    ADD COLUMN IF NOT EXISTS complete_photos TEXT[],
    ADD COLUMN IF NOT EXISTS receive_notes   TEXT,
    ADD COLUMN IF NOT EXISTS complete_notes  TEXT;

COMMENT ON COLUMN outsource_orders.receive_photos  IS '加工方接收物料时的拍照证据（多张，相对路径）';
COMMENT ON COLUMN outsource_orders.complete_photos IS '加工方加工完成交货前的成品拍照证据';
COMMENT ON COLUMN outsource_orders.receive_notes   IS '接收时的备注（物料状态说明）';
COMMENT ON COLUMN outsource_orders.complete_notes  IS '完工时的备注（加工状态说明）';

-- material_shipments 的 photo_paths 已经是 TEXT[]，加个备注强调用途
COMMENT ON COLUMN material_shipments.photo_paths IS '材料方发货时的拍照证据（多张，相对路径；用于后续与加工方收料照对比）';

SELECT 'outsource_orders 新列' AS item,
       (SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name='outsource_orders'
          AND column_name IN ('receive_photos','complete_photos','receive_notes','complete_notes')) AS cnt;
