# Implementation Plan: Part Selection and Drawing Library

## Overview

Implement three interconnected features: (1) materials dictionary with standardized part material selection, (2) process method multi-select from existing `process_methods` table, and (3) a tenant-scoped drawing library with project linking. The implementation follows existing FastAPI + psycopg2 patterns, adds new route files for materials and drawings, and modifies the existing part creation flow for backward-compatible structured input.

## Tasks

- [x] 1. Database migration and seed data
  - [x] 1.1 Create migration file `backend/database/migrations/013_part_selection_drawing_library.sql`
    - Create `materials` table (id, code UNIQUE, name, category, remark, is_active, created_at)
    - Add `material_id INTEGER REFERENCES materials(id)` column to `project_parts`
    - Create `drawing_library` table (id, tenant_id, name, description, file_name, file_path, file_size, mime_type, category, tags TEXT[], uploaded_by, created_at, updated_at)
    - Create `project_drawing_links` table (id, project_id, drawing_id, linked_by, linked_at, UNIQUE(project_id, drawing_id))
    - Create all indexes (idx_materials_category, idx_materials_active, idx_pp_material, idx_dl_tenant, idx_dl_category, idx_pdl_project, idx_pdl_drawing)
    - Insert seed data for common mold steels (Cr12MoV, SKD11, DC53, SKD61, H13, S136, NAK80, 718H, P20, 45#) with ON CONFLICT DO NOTHING
    - _Requirements: 1.1, 1.2, 4.1, 5.1_

- [x] 2. Materials admin backend
  - [x] 2.1 Create `backend/mvp/routes/materials.py` with materials CRUD endpoints
    - GET `/api/internal/materials` — list active materials ordered by (category, code) for dropdown use
    - GET `/api/internal/admin/materials` — list all materials including inactive (admin view)
    - POST `/api/internal/admin/materials` — create material with `MaterialCreate` model validation
    - PATCH `/api/internal/admin/materials/{id}` — update material with `MaterialUpdate` model
    - DELETE `/api/internal/admin/materials/{id}` — delete material only if unreferenced by project_parts (HTTP 409 if referenced)
    - Handle duplicate code with HTTP 409
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 2.1_

  - [x] 2.2 Register materials router in `backend/mvp/main.py`
    - Import and include the new materials router
    - _Requirements: 1.3, 2.1_

- [x] 3. Drawing library backend
  - [x] 3.1 Create `backend/mvp/routes/drawings.py` with drawing library CRUD endpoints
    - GET `/api/internal/drawing-library` — list tenant drawings with optional category filter and keyword search on name
    - POST `/api/internal/drawing-library` — upload file to `backend/uploads/library/`, create drawing_library record scoped to user's tenant
    - GET `/api/internal/drawing-library/{id}` — get drawing detail (tenant-scoped)
    - PATCH `/api/internal/drawing-library/{id}` — update metadata (name, description, category, tags)
    - DELETE `/api/internal/drawing-library/{id}` — delete drawing only if unlinked from projects (HTTP 409 if linked, listing which projects reference it)
    - GET `/api/internal/drawing-library/{id}/download` — serve file with correct Content-Type
    - Enforce tenant isolation on all queries (WHERE tenant_id = user.tenant_id)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 3.2 Add project drawing link endpoints in `backend/mvp/routes/drawings.py`
    - GET `/api/internal/projects/{project_id}/drawing-links` — list linked drawings with metadata
    - POST `/api/internal/projects/{project_id}/drawing-links` — link drawings (validate drawing_ids exist and belong to user's tenant, reject if project not in "drafted" status with HTTP 409)
    - DELETE `/api/internal/projects/{project_id}/drawing-links/{link_id}` — unlink drawing (reject if project not in "drafted" status with HTTP 409)
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [x] 3.3 Register drawings router in `backend/mvp/main.py`
    - Import and include the new drawings router
    - _Requirements: 4.2, 5.2_

- [x] 4. Modified part creation with material_id and process_method_ids
  - [x] 4.1 Update `PartCreate` model and `add_part` endpoint in `backend/mvp/routes/internal.py`
    - Add `material_id: Optional[int]` and `process_method_ids: Optional[list[int]]` to `PartCreate`
    - When `material_id` is provided: validate it exists in materials table, auto-populate `material` text column with the material code, store `material_id` FK
    - When `material_id` is not provided: use existing `material` text field as-is (backward compatible)
    - When `process_method_ids` is provided: validate all IDs exist in `process_methods`, create `project_processes` records with sequential seq_no, ignore `processes` text field
    - When `process_method_ids` is not provided: use existing `processes` text list logic (backward compatible)
    - Return HTTP 400 with descriptive error for invalid material_id or process_method_ids
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 4.2 Update `get_project` endpoint in `backend/mvp/routes/internal.py` to include drawing_links
    - Join `project_drawing_links` and `drawing_library` to include `drawing_links` array in project detail response
    - Each link entry includes: id, drawing_id, name, file_name, category, linked_at
    - Distinguish from existing `attachments` in the response structure
    - _Requirements: 6.1, 6.2_

- [x] 5. Checkpoint - Ensure all backend endpoints work
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Frontend: materials admin page
  - [x] 6.1 Create `backend/static/internal/materials-admin.html`
    - Table listing all materials (code, name, category, is_active, actions)
    - Add modal with form fields (code, name, category, remark)
    - Edit modal pre-populated with existing values
    - Delete button with confirmation (handle 409 error gracefully)
    - Toggle active/inactive status
    - _Requirements: 1.3, 1.4, 1.5, 1.6_

- [x] 7. Frontend: drawing library page
  - [x] 7.1 Create `backend/static/internal/drawing-library.html`
    - Table listing tenant drawings (name, file_name, category, tags, uploaded date, actions)
    - Category filter dropdown and name search input
    - Upload modal (file input + name + description + category + tags)
    - Edit metadata modal
    - Delete button with confirmation (handle 409 error gracefully showing linked projects)
    - Download link per row
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

- [x] 8. Frontend: project detail page modifications
  - [x] 8.1 Update part form in `backend/static/internal/project-detail.html`
    - Replace material text input with `<select>` dropdown populated from GET `/api/internal/materials`
    - Replace process text input with multi-select checkboxes/tags from GET `/api/internal/process-methods`
    - Submit `material_id` and `process_method_ids` in part creation payload instead of free text
    - _Requirements: 2.1, 2.2, 3.1, 3.2_

  - [x] 8.2 Add drawing library links section in `backend/static/internal/project-detail.html`
    - New "图纸库关联" section below existing attachments
    - Display linked drawings table (name, file_name, category, linked_at, actions)
    - "关联图纸" button opens modal to search/filter and select drawings from library
    - "取消关联" button per row to unlink (only when project is drafted)
    - Handle 409 errors when project is not in drafted status
    - _Requirements: 5.2, 5.4, 5.5, 5.7, 5.8, 6.1, 6.2, 6.3_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ]* 10. Property-based tests
  - [ ]* 10.1 Write property test for materials list ordering
    - **Property 1: Materials list ordering**
    - Generate random materials with various categories and codes, verify GET returns only active materials sorted by (category ASC, code ASC)
    - **Validates: Requirements 2.1**

  - [ ]* 10.2 Write property test for material ID validation and denormalization
    - **Property 2: Material ID validation and denormalization**
    - Generate valid/invalid material_ids, verify part creation stores FK and auto-populates material text column, or rejects with 400
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5**

  - [ ]* 10.3 Write property test for process method IDs validation
    - **Property 3: Process method IDs validation and record creation**
    - Generate valid/invalid ID arrays, verify correct project_processes records created with sequential seq_no, or rejection with invalid IDs listed
    - **Validates: Requirements 3.2, 3.3, 3.4**

  - [ ]* 10.4 Write property test for process method IDs precedence
    - **Property 4: Process method IDs precedence**
    - Generate requests with both process_method_ids and processes fields, verify only IDs are used
    - **Validates: Requirements 3.6**

  - [ ]* 10.5 Write property test for drawing library tenant isolation
    - **Property 5: Drawing library tenant isolation**
    - Generate multi-tenant drawing data, verify each tenant only sees their own drawings
    - **Validates: Requirements 4.3, 4.8**

  - [ ]* 10.6 Write property test for drawing link validation
    - **Property 6: Drawing link validation**
    - Generate valid/invalid/cross-tenant drawing_ids, verify correct link creation or rejection
    - **Validates: Requirements 5.2, 5.3**

  - [ ]* 10.7 Write property test for link deletion preserves drawing
    - **Property 7: Link deletion preserves drawing**
    - Delete project_drawing_links, verify drawing_library records remain unchanged
    - **Validates: Requirements 5.5**

  - [ ]* 10.8 Write property test for drawing multi-project linking
    - **Property 8: Drawing multi-project linking**
    - Link one drawing to N projects, verify it appears in all project queries
    - **Validates: Requirements 5.6**

  - [ ]* 10.9 Write property test for non-drafted project rejects link modifications
    - **Property 9: Non-drafted project rejects link modifications**
    - Generate non-drafted project statuses, verify POST/DELETE on drawing-links returns 409
    - **Validates: Requirements 5.7, 5.8**

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The design uses Python (FastAPI + psycopg2) — no language selection needed
- Migration (task 1) must be run before any backend tasks
- Property tests validate universal correctness properties from the design document
- Drawing files stored in `backend/uploads/library/` (separate from per-project `uploads/drawings/`)
- Tenant isolation enforced at query level on all drawing library operations
