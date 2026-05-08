-- =============================================================================
-- Migration 009: 简化角色
-- =============================================================================
-- 方案 B：我方内部不再分细角色，全部统一 admin。
-- - bob/carol/dave 保留为"岗位名"，但 role 全部设为 admin
-- - roles 表只留 ADMIN / OPERATOR 两条
-- =============================================================================

UPDATE users SET role = 'admin'
WHERE username IN ('alice', 'bob', 'carol', 'dave')
   OR role IN ('production_manager','purchasing_manager','project_manager',
                'quality_manager','technical_director');

-- workflow_approval_tasks 里 role 的过滤字段也升级：
-- 旧的以 'PRODUCTION_MANAGER' / 'PURCHASING_MANAGER' 为 assignee_id 的待办，
-- 现在所有 internal admin 都能看
UPDATE workflow_approval_tasks
SET assignee_id = 'ADMIN'
WHERE assignee_type = 'role'
  AND assignee_id IN ('PRODUCTION_MANAGER','PURCHASING_MANAGER','QUALITY_MANAGER','PROJECT_MANAGER','TECHNICAL_DIRECTOR');

-- 字典精简（保留老条目作历史参考，但都停用）
DELETE FROM roles WHERE code NOT IN ('ADMIN','OPERATOR');

SELECT username, display_name, role FROM users ORDER BY id;
