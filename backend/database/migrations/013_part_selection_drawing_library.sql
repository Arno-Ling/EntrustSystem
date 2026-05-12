-- =============================================================================
-- Migration 013: 零件材质选择 + 图纸存储库
-- =============================================================================
-- 1. materials 字典表 + 种子数据
-- 2. project_parts 加 material_id FK
-- 3. drawing_library 图纸库表
-- 4. project_drawing_links 关联表
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. materials 材质字典表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS materials (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(64) NOT NULL UNIQUE,
    name        VARCHAR(128) NOT NULL,
    category    VARCHAR(64),
    remark      TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_materials_category ON materials (category);
CREATE INDEX IF NOT EXISTS idx_materials_active ON materials (is_active);

COMMENT ON TABLE materials IS '材质字典表（零件添加时单选）';
COMMENT ON COLUMN materials.code IS '材质编码，如 Cr12MoV、SKD11';
COMMENT ON COLUMN materials.category IS '分类：cold_work / hot_work / plastic_mold / structural';


-- -----------------------------------------------------------------------------
-- 2. project_parts 加 material_id
-- -----------------------------------------------------------------------------
ALTER TABLE project_parts
    ADD COLUMN IF NOT EXISTS material_id INTEGER REFERENCES materials(id);

CREATE INDEX IF NOT EXISTS idx_pp_material ON project_parts (material_id);


-- -----------------------------------------------------------------------------
-- 3. drawing_library 图纸存储库
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drawing_library (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    file_name   VARCHAR(255) NOT NULL,
    file_path   VARCHAR(512) NOT NULL,
    file_size   BIGINT NOT NULL,
    mime_type   VARCHAR(128) NOT NULL,
    category    VARCHAR(64),
    tags        TEXT[] DEFAULT '{}',
    uploaded_by INTEGER REFERENCES users(id),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dl_tenant ON drawing_library (tenant_id);
CREATE INDEX IF NOT EXISTS idx_dl_category ON drawing_library (tenant_id, category);

COMMENT ON TABLE drawing_library IS '图纸存储库（租户级别，可跨项目复用）';

-- updated_at 自动更新触发器
DROP TRIGGER IF EXISTS tr_dl_updated_at ON drawing_library;
CREATE TRIGGER tr_dl_updated_at BEFORE UPDATE ON drawing_library
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();


-- -----------------------------------------------------------------------------
-- 4. project_drawing_links 项目-图纸关联
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_drawing_links (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    drawing_id  INTEGER NOT NULL REFERENCES drawing_library(id) ON DELETE RESTRICT,
    linked_by   INTEGER REFERENCES users(id),
    linked_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, drawing_id)
);

CREATE INDEX IF NOT EXISTS idx_pdl_project ON project_drawing_links (project_id);
CREATE INDEX IF NOT EXISTS idx_pdl_drawing ON project_drawing_links (drawing_id);

COMMENT ON TABLE project_drawing_links IS '项目关联图纸库图纸（引用方式，非复制）';


-- -----------------------------------------------------------------------------
-- 5. 种子数据：常用模具钢
-- -----------------------------------------------------------------------------
INSERT INTO materials (code, name, category) VALUES
    ('Cr12MoV', 'Cr12MoV 冷作模具钢', 'cold_work'),
    ('SKD11',   'SKD11 冷作模具钢',   'cold_work'),
    ('DC53',    'DC53 冷作模具钢',     'cold_work'),
    ('SKD61',   'SKD61 热作模具钢',   'hot_work'),
    ('H13',     'H13 热作模具钢',     'hot_work'),
    ('S136',    'S136 防腐镜面钢',    'plastic_mold'),
    ('NAK80',   'NAK80 预硬塑胶钢',   'plastic_mold'),
    ('718H',    '718H 预硬塑胶钢',    'plastic_mold'),
    ('P20',     'P20 预硬塑胶钢',     'plastic_mold'),
    ('45#',     '45# 碳素结构钢',     'structural')
ON CONFLICT (code) DO NOTHING;


-- -----------------------------------------------------------------------------
-- 完成校验
-- -----------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='public' AND table_name IN ('materials','drawing_library','project_drawing_links')) AS new_tables,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='project_parts' AND column_name='material_id') AS pp_col,
  (SELECT COUNT(*) FROM materials) AS seed_count;
-- 期望: new_tables=3, pp_col=1, seed_count>=10
