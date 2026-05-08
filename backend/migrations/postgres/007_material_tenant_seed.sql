-- =============================================================================
-- Migration 007: 材料方租户 + 测试账号
-- =============================================================================

-- 1) 添加 2 家材料方到 suppliers（如果未创建）
INSERT INTO suppliers (name, category, address, contact_name, contact_phone) VALUES
  ('上海宝钢模具钢贸易有限公司', '模具钢',   '上海市松江区九亭镇',     '杨经理', '13801234567'),
  ('东莞鑫盛五金材料有限公司',   '五金/标准件','广东省东莞市长安镇',   '吴经理', '13902345678')
ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category;

-- 2) tenants 增加 material 类型
INSERT INTO tenants (tenant_type, name, supplier_id, contact_name, contact_phone) VALUES
  ('material', '上海宝钢模具钢贸易有限公司',
     (SELECT id FROM suppliers WHERE name='上海宝钢模具钢贸易有限公司'),
     '杨经理', '13801234567'),
  ('material', '东莞鑫盛五金材料有限公司',
     (SELECT id FROM suppliers WHERE name='东莞鑫盛五金材料有限公司'),
     '吴经理', '13902345678')
ON CONFLICT (name) DO UPDATE SET
  tenant_type = EXCLUDED.tenant_type,
  supplier_id = EXCLUDED.supplier_id,
  contact_name = EXCLUDED.contact_name,
  contact_phone = EXCLUDED.contact_phone;

-- 3) 用户（密码 test123，bcrypt hash 复用已有）
INSERT INTO users (tenant_id, username, password_hash, display_name, role, is_active) VALUES
  ((SELECT id FROM tenants WHERE name='上海宝钢模具钢贸易有限公司'),
     'baogang', '$2b$12$tlvx8/TCrANPZ66qSIy8m./pw8yN6PgJ0dKRzhMD3kLk69/fnjXNi',
     '宝钢-杨经理', 'operator', TRUE),
  ((SELECT id FROM tenants WHERE name='东莞鑫盛五金材料有限公司'),
     'xinsheng', '$2b$12$tlvx8/TCrANPZ66qSIy8m./pw8yN6PgJ0dKRzhMD3kLk69/fnjXNi',
     '鑫盛-吴经理', 'operator', TRUE)
ON CONFLICT (username) DO UPDATE SET
  tenant_id = EXCLUDED.tenant_id,
  password_hash = EXCLUDED.password_hash,
  display_name = EXCLUDED.display_name,
  role = EXCLUDED.role;

-- 4) 给 2 家新材料方关联价格（示例）
INSERT INTO material_prices (material_code, material_name, spec, unit, unit_price, supplier_id, valid_from, valid_to, source, remark) VALUES
  ('Cr12MoV', 'Cr12MoV 工具钢', '200x100x50','kg', 66.00,
     (SELECT id FROM suppliers WHERE name='上海宝钢模具钢贸易有限公司'),
     '2026-01-01', '2026-12-31', 'contract', '宝钢合同价'),
  ('SKD61',   'SKD61 热作钢', 'φ100x500','kg', 93.50,
     (SELECT id FROM suppliers WHERE name='上海宝钢模具钢贸易有限公司'),
     '2026-01-01', '2026-08-31', 'contract', '宝钢合同价')
ON CONFLICT (material_code, spec, supplier_id, valid_from) DO NOTHING;

SELECT username, display_name, tenant_type
FROM users u JOIN tenants t ON u.tenant_id=t.id
WHERE t.tenant_type = 'material'
ORDER BY username;
