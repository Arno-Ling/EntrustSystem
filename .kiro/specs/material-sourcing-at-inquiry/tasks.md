# Implementation Plan: Material Sourcing at Inquiry Stage

## Overview

Move the material sourcing decision from the outsource order stage to the outsource request stage. Modify the broadcast endpoint to atomically create both processor invitations and (when mode='internal') a multi-line material inquiry with invitations to material suppliers. Update processor quoting to support `material_unit_price` per line, and material supplier quoting to support per-line quotations.

## Tasks

- [x] 1. Database migration: schema changes for material sourcing at inquiry stage
  - [x] 1.1 Create migration file `backend/database/migrations/012_material_sourcing_at_inquiry.sql`
    - Add `material_sourcing VARCHAR(16)` column to `outsource_requests` table
    - Create `material_inquiry_lines` table (id, inquiry_id FK, material_code, material_name, spec, qty, unit, sort_order, created_at)
    - Add `outsource_request_id INTEGER` FK column to `material_inquiries` table
    - Make `outsource_order_id` nullable on `material_inquiries` (ALTER COLUMN DROP NOT NULL)
    - Add `material_unit_price DECIMAL(14,2)` nullable column to `outsource_quotation_lines`
    - Add `inquiry_line_id INTEGER` FK column to `material_quotations`
    - Drop existing UNIQUE constraint on `material_quotations.invitation_id`, replace with UNIQUE (invitation_id, inquiry_line_id)
    - Create appropriate indexes (idx_mil_inquiry, idx_mi_request)
    - _Requirements: 1.1, 1.4, 2.1, 3.1, 3.2, 6.1, 7.1_

- [x] 2. Backend: modify broadcast endpoint to support material sourcing
  - [x] 2.1 Update `POST /api/internal/outsource-requests/{req_id}/send` in `backend/mvp/routes/internal.py`
    - Add validation: reject with 400 if `outsource_requests.material_sourcing` is NULL ("请先选择材料供应方式")
    - Keep existing processor invitation logic unchanged
    - When `material_sourcing='internal'`: create one `material_inquiries` record with `outsource_request_id` set, `outsource_order_id` NULL
    - Extract distinct `(material, spec)` from `project_parts` for the request's project → create `material_inquiry_lines` rows
    - If no materials found in BOM, reject with 400 ("项目零件清单中没有材料信息，无法创建材料询价")
    - Create `material_inquiry_invitations` for all material-type tenants with linked suppliers
    - Set material inquiry status to 'inviting', record `sent_at` on invitations
    - Wrap everything (processor invitations + material inquiry) in a single transaction
    - Return enriched response: `invited_processor_count`, `material_inquiry_id`, `material_inquiry_no`, `invited_material_supplier_count` (or NULL when processor mode)
    - _Requirements: 1.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 10.1, 10.2, 10.3_

  - [x] 2.2 Add `PATCH /api/internal/outsource-requests/{req_id}` support for `material_sourcing` field
    - Allow setting `material_sourcing` only when request status is 'draft'
    - Validate value is 'internal' or 'processor'
    - _Requirements: 1.2, 9.2_

  - [ ]* 2.3 Write property tests for broadcast logic (Properties 1, 2, 3, 12)
    - **Property 1: Broadcast in internal mode creates inquiry with correct BOM-derived lines**
    - **Property 2: Broadcast in processor mode creates no material inquiry**
    - **Property 3: Broadcast in internal mode invites all material tenants**
    - **Property 12: Broadcast response contains correct counts**
    - **Validates: Requirements 2.4, 4.1, 4.2, 4.3, 4.5, 10.1**

- [x] 3. Backend: modify processor invitation detail and quoting
  - [x] 3.1 Update `GET /api/processor/invitations/{inv_id}` in `backend/mvp/routes/processor.py`
    - Join through `outsource_requests` to include `material_sourcing` field in response
    - _Requirements: 5.1, 5.2_

  - [x] 3.2 Update `POST /api/processor/invitations/{inv_id}/quote` in `backend/mvp/routes/processor.py`
    - Add `material_unit_price: Optional[float]` to `QuoteLine` model
    - Look up parent request's `material_sourcing` value
    - If `material_sourcing='processor'` and lines mode: validate `material_unit_price > 0` for each line, reject with 400 if missing
    - If `material_sourcing='internal'`: ignore submitted `material_unit_price`, store as NULL
    - Persist `material_unit_price` to `outsource_quotation_lines`
    - _Requirements: 6.2, 6.3, 6.4, 6.5_

  - [ ]* 3.3 Write property tests for processor quoting (Properties 4, 5, 6)
    - **Property 4: Invitation detail API includes material_sourcing from parent request**
    - **Property 5: Processor quote in processor mode requires valid material_unit_price**
    - **Property 6: Processor quote in internal mode nullifies material_unit_price**
    - **Validates: Requirements 5.1, 5.2, 6.2, 6.3, 6.4**

- [x] 4. Backend: material supplier per-line quoting
  - [x] 4.1 Add multi-line quote models and endpoint in `backend/mvp/routes/inquiry.py`
    - Add `MaterialQuoteLine` and `MaterialQuoteSubmitMultiLine` Pydantic models
    - Create or modify `POST /api/material/inquiries/{inv_id}/quote` to accept `lines: [{inquiry_line_id, unit_price, lead_time_days, note}]`
    - Validate each `inquiry_line_id` belongs to the inquiry associated with the invitation
    - Upsert: use UNIQUE (invitation_id, inquiry_line_id) for per-line quoting
    - Aggregate total price = sum(unit_price × qty) across quoted lines
    - Keep backward compatibility: if no `lines` field, use existing single-quote logic
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 4.2 Update `GET /api/material/inquiries/{inv_id}` to return inquiry lines
    - Query `material_inquiry_lines` for the inquiry and include in response
    - Include existing per-line quotations from the current supplier
    - _Requirements: 2.2, 2.3_

  - [ ]* 4.3 Write property tests for material supplier quoting (Properties 7, 8, 9)
    - **Property 7: Material supplier per-line quoting with validation**
    - **Property 8: Material quotation upsert idempotence**
    - **Property 9: Material quotation line aggregation**
    - **Validates: Requirements 7.2, 7.3, 7.4, 7.5**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Backend: award flow links material inquiries to new order
  - [x] 6.1 Update `award_outsource` in `backend/mvp/routes/internal.py`
    - After creating `outsource_orders`, copy `material_sourcing` from `outsource_requests` to the new order
    - Find all `material_inquiries` where `outsource_request_id = request_id`
    - Update their `outsource_order_id` to the newly created order id
    - If no material inquiries exist, proceed without error
    - _Requirements: 3.4, 8.1, 8.2, 8.3, 8.4_

  - [ ]* 6.2 Write property tests for award flow (Properties 10, 11)
    - **Property 10: Award links material inquiries to new order**
    - **Property 11: Award propagates material_sourcing to order**
    - **Validates: Requirements 3.4, 8.1, 8.2, 8.3**

- [x] 7. Frontend: outsource request detail page — sourcing mode selection
  - [x] 7.1 Update `backend/static/internal/outsource-request-detail.html`
    - Add radio button group for material sourcing mode: "我方统一供料 (internal)" / "加工方自采 (processor)"
    - Show radio buttons only when request status is 'draft'; show read-only badge otherwise
    - On selection change, PATCH to `/api/internal/outsource-requests/{id}` with `material_sourcing` value
    - Before broadcast button click: validate `material_sourcing` is set, show error "请先选择材料供应方式" if NULL
    - After successful broadcast with internal mode: show toast with material inquiry info (inquiry_no, invited count)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.1_

- [x] 8. Frontend: processor invitation detail page — sourcing notice and material price input
  - [x] 8.1 Update `backend/static/processor/invitation-detail.html`
    - Display material sourcing notice banner based on `material_sourcing` field from API response
    - Internal mode: "📦 材料由我方统一供应，无需报材料费"
    - Processor mode: "🛒 材料由贵方自行采购，请在报价中包含材料成本"
    - In line-by-line quote mode with `processor` sourcing: add `material_unit_price` input column to the quote table
    - Validate `material_unit_price > 0` before submit when mode = processor
    - _Requirements: 5.3, 5.4, 6.2, 6.3_

- [x] 9. Frontend: material supplier multi-line inquiry quote page
  - [x] 9.1 Create `backend/static/material/inquiry-detail.html`
    - Show inquiry header info (inquiry_no, title, project)
    - Display material inquiry lines in a table (material_code, spec, qty, unit)
    - Per-line input fields: unit_price, lead_time_days, note
    - Submit button posts array of line quotations to `POST /api/material/inquiries/{inv_id}/quote`
    - Show existing quotations if already submitted (allow update)
    - _Requirements: 7.2, 7.3_

  - [x] 9.2 Update `backend/static/material/home.html` to link to inquiry detail page
    - Add inquiry invitations list or link to the material supplier home page
    - _Requirements: 7.2_

- [x] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The design uses Python (FastAPI + psycopg2) throughout — no language selection needed
- The migration (task 1) must be run before any backend tasks
- Property tests validate universal correctness properties from the design document
- All database operations in the broadcast endpoint must be wrapped in a single transaction for atomicity (Requirement 4.6, 10.3)
