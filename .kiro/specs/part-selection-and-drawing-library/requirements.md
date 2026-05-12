# Requirements Document

## Introduction

本需求文档描述三个相互关联的功能增强：

1. **零件材质单选** — 添加零件时，材质从字典表中单选（取代自由文本输入）
2. **零件工序多选** — 添加零件时，工序从 `process_methods` 字典表中多选（取代自由文本列表）
3. **图纸存储库** — 系统管理区新增集中式图纸库，支持 CRUD 管理；创建/编辑项目时可从图纸库选择图纸关联到项目（引用方式，非复制）

## Glossary

- **System**: 模具委外采购管理系统整体
- **Materials_Dictionary**: 材质字典表（`materials`），存储所有可选材质编码与名称
- **Process_Methods**: 加工方式字典表（`process_methods`），已有表，存储所有可选工序
- **Drawing_Library**: 图纸存储库，租户级别的集中式图纸管理模块
- **Drawing_Entry**: 图纸库中的一条记录，包含文件元数据和上传的文件
- **Project**: 项目/订单，对应 `projects` 表中的一条记录
- **Part**: 零件，对应 `project_parts` 表中的一条记录
- **Tenant**: 租户，系统中的组织单位（internal / processor / material）
- **Internal_User**: 我方（internal 类型租户）的用户

## Requirements

### Requirement 1: 材质字典表管理

**User Story:** As an Internal_User (admin), I want to maintain a materials dictionary so that all parts reference standardized material names.

#### Acceptance Criteria

1. THE System SHALL provide a `materials` dictionary table with columns: id, code (unique), name, category, remark, is_active, created_at
2. WHEN the System is initialized, THE Materials_Dictionary SHALL contain seed data including common mold steels (Cr12MoV, SKD11, SKD61, S136, NAK80, 45#, P20, 718H, H13, DC53)
3. WHEN an Internal_User with admin role sends a POST request to the materials admin endpoint, THE System SHALL create a new material entry and return the created record
4. WHEN an Internal_User with admin role sends a PATCH request to the materials admin endpoint, THE System SHALL update the specified material entry
5. WHEN an Internal_User with admin role sends a DELETE request to a material that is not referenced by any project_parts record, THE System SHALL delete the material entry
6. IF an Internal_User attempts to delete a material that is referenced by existing project_parts records, THEN THE System SHALL reject the deletion with HTTP 409 and a descriptive error message

### Requirement 2: 零件添加时材质单选

**User Story:** As an Internal_User, I want to select a material from a dropdown when adding a part so that material data is consistent and standardized.

#### Acceptance Criteria

1. THE System SHALL expose a GET endpoint `/api/internal/materials` that returns all active materials from the Materials_Dictionary ordered by category and code
2. WHEN an Internal_User creates a part with a `material_id` field, THE System SHALL validate that the material_id exists in the Materials_Dictionary and store the reference
3. IF an Internal_User provides a material_id that does not exist in the Materials_Dictionary, THEN THE System SHALL reject the request with HTTP 400 and a descriptive error message
4. THE System SHALL continue to store the material text value in `project_parts.material` column (denormalized) for display and backward compatibility
5. WHEN the part creation payload includes `material_id`, THE System SHALL look up the material code from the Materials_Dictionary and populate the `project_parts.material` column automatically

### Requirement 3: 零件添加时工序多选

**User Story:** As an Internal_User, I want to select multiple processes from the process_methods dictionary when adding a part so that process data is structured and validated.

#### Acceptance Criteria

1. THE System SHALL accept a `process_method_ids` field (array of integers) in the part creation payload as an alternative to the free-text `processes` field
2. WHEN an Internal_User creates a part with `process_method_ids`, THE System SHALL validate that each id exists in the Process_Methods table
3. IF any process_method_id does not exist in the Process_Methods table, THEN THE System SHALL reject the request with HTTP 400 indicating which ids are invalid
4. WHEN valid `process_method_ids` are provided, THE System SHALL create corresponding records in the `project_processes` table with auto-generated seq_no and process_code
5. THE System SHALL continue to support the legacy free-text `processes` field for backward compatibility
6. WHEN both `process_method_ids` and `processes` are provided in the same request, THE System SHALL use `process_method_ids` and ignore the free-text `processes` field

### Requirement 4: 图纸库 CRUD 管理

**User Story:** As an Internal_User (admin), I want to manage a centralized drawing library so that drawings can be reused across multiple projects.

#### Acceptance Criteria

1. THE System SHALL provide a `drawing_library` table with columns: id, tenant_id, name, description, file_name, file_path, file_size, mime_type, category, tags (text array), uploaded_by, created_at, updated_at
2. WHEN an Internal_User sends a POST request with a file upload to `/api/internal/drawing-library`, THE System SHALL store the file in the server filesystem and create a Drawing_Entry record scoped to the user's tenant
3. WHEN an Internal_User sends a GET request to `/api/internal/drawing-library`, THE System SHALL return all Drawing_Entry records belonging to the user's tenant, supporting optional filtering by category and keyword search on name
4. WHEN an Internal_User sends a GET request to `/api/internal/drawing-library/{id}`, THE System SHALL return the Drawing_Entry detail including download URL
5. WHEN an Internal_User sends a PATCH request to `/api/internal/drawing-library/{id}`, THE System SHALL update the metadata (name, description, category, tags) of the Drawing_Entry
6. WHEN an Internal_User sends a DELETE request to a Drawing_Entry that is not linked to any project, THE System SHALL delete the file from the filesystem and remove the database record
7. IF an Internal_User attempts to delete a Drawing_Entry that is currently linked to one or more projects, THEN THE System SHALL reject the deletion with HTTP 409 and indicate which projects reference the drawing
8. THE Drawing_Library SHALL be scoped per tenant — each tenant can only view and manage drawings belonging to their own tenant

### Requirement 5: 项目关联图纸库图纸

**User Story:** As an Internal_User, I want to link drawings from the drawing library to a project so that I can reuse existing drawings without re-uploading.

#### Acceptance Criteria

1. THE System SHALL provide a `project_drawing_links` junction table with columns: id, project_id, drawing_id, linked_by, linked_at
2. WHEN an Internal_User sends a POST request to `/api/internal/projects/{project_id}/drawing-links` with a list of drawing_ids, THE System SHALL create link records associating the drawings with the project
3. IF any drawing_id does not exist or does not belong to the user's tenant, THEN THE System SHALL reject the request with HTTP 400 and indicate which ids are invalid
4. WHEN an Internal_User sends a GET request to `/api/internal/projects/{project_id}/drawing-links`, THE System SHALL return all drawings linked to the project from the drawing library (with metadata: name, file_name, category, linked_at)
5. WHEN an Internal_User sends a DELETE request to `/api/internal/projects/{project_id}/drawing-links/{link_id}`, THE System SHALL remove the link without deleting the Drawing_Entry from the library
6. THE System SHALL allow a single Drawing_Entry to be linked to multiple projects simultaneously
7. WHILE a project is in "drafted" status, THE System SHALL allow adding and removing drawing links
8. IF a project is not in "drafted" status, THEN THE System SHALL reject attempts to add or remove drawing links with HTTP 409

### Requirement 6: 项目详情展示关联图纸

**User Story:** As an Internal_User, I want to see both uploaded attachments and linked library drawings in the project detail view so that I have a complete picture of all project drawings.

#### Acceptance Criteria

1. WHEN an Internal_User retrieves a project detail via GET `/api/internal/projects/{project_id}`, THE System SHALL include a `drawing_links` array in the response containing all linked Drawing_Entry records with their metadata
2. THE System SHALL distinguish between directly uploaded attachments (existing `attachments` table) and library-linked drawings (`project_drawing_links`) in the project detail response
3. WHEN a Drawing_Entry file is requested for download, THE System SHALL serve the file from the drawing library storage path with appropriate Content-Type headers
