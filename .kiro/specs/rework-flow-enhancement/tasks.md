# Implementation Plan: Rework Flow Enhancement (返工流程增强)

## Overview

Enhance the rework flow so processors can confirm rework, upload delivery photos, and advance status. The system auto-branches based on `material_sourcing` (Branch A: internal creates MPO, Branch B: processor self-sources). Adds cost tracking fields and internal inspect/complete endpoints.

## Tasks

- [x] 1. Database migration
  - [x] 1.1 Create migration file `backend/database/migrations/014_rework_flow_enhancement.sql`
    - ALTER TABLE `rework_orders` to add columns: `confirmed_at`, `confirmed_by`, `confirm_remark`, `delivered_at`, `deliver_photos` (JSONB DEFAULT '[]'), `deliver_remark`, `inspected_at`, `completed_at`, `material_sourcing_mode` (VARCHAR(16)), `cost_bearer` (VARCHAR(32) DEFAULT 'processor'), `cost_amount` (DECIMAL(14,2)), `cost_remark`, `new_po_id` (FK to material_purchase_orders)
    - Add COMMENT ON COLUMN for each new field
    - Use `ADD COLUMN IF NOT EXISTS` for idempotency
    - _Requirements: 6.1, 9.4, 2.3, 2.5_

- [x] 2. Backend — Rework service (branching logic)
  - [x] 2.1 Create `backend/mvp/rework_service.py` with `process_rework_confirmation(rework_order_id, conn)` function
    - Query original outsource order's `material_sourcing` (fallback to outsource_requests.material_sourcing)
    - Set `material_sourcing_mode` and `cost_bearer='processor'` on rework order
    - Branch A (internal): call `create_rework_material_po()` and set `new_po_id`
    - Branch B (processor): no MPO creation
    - Transition rework order status to `in_progress`
    - Return `{branch: 'A'|'B', new_po_id: int|None}`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3_

  - [x] 2.2 Implement `create_rework_material_po(rework, original_order, conn)` in same file
    - Find original MPO via `rework.original_po_id` or fallback to project-level lookup
    - Generate PO number `MP-RW-{suffix}-{hex}`
    - Copy `material_code`, `spec`, `qty`, `supplier_id`, `unit`, `unit_price` from original MPO
    - Set remark = '异常返工补料 - {rework_no}', status = 'drafted'
    - Handle error case: no original MPO found → leave rework in 'confirmed', flag for manual intervention
    - _Requirements: 2.1, 2.2, 2.6_

  - [ ]* 2.3 Write property tests for Branch A/B logic
    - **Property 3: Branch A confirmation creates a valid MPO copy**
    - **Property 4: Branch B confirmation creates no MPO**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3**

- [-] 3. Backend — Processor rework endpoints
  - [x] 3.1 Add `REWORK_TRANSITIONS` dict and Pydantic models (`ReworkConfirm`, `ReworkDeliver`) to `backend/mvp/routes/processor.py`
    - Define valid transitions: pending→confirmed, confirmed→in_progress, in_progress→delivered, delivered→inspecting, inspecting→completed, any non-terminal→cancelled
    - _Requirements: 9.1, 9.2_

  - [x] 3.2 Implement `GET /api/processor/rework-orders` endpoint
    - List rework orders belonging to the processor's tenant (via original_order_id → outsource_orders.tenant_id)
    - Include status, rework_no, project info, timestamps
    - _Requirements: 1.1_

  - [x] 3.3 Implement `GET /api/processor/rework-orders/{rework_id}` endpoint
    - Return rework order detail with original order info, material_sourcing_mode, cost fields, deliver_photos, timeline
    - Validate tenant ownership
    - _Requirements: 1.1, 1.5_

  - [x] 3.4 Implement `POST /api/processor/rework-orders/{rework_id}/confirm` endpoint
    - Validate status == 'pending' (409 otherwise)
    - Validate tenant ownership (403 otherwise)
    - Set confirmed_at, confirmed_by, confirm_remark
    - Call `process_rework_confirmation()` from rework_service
    - Return branch info and new status
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 2.1, 2.4, 3.1_

  - [x] 3.5 Implement `POST /api/processor/rework-orders/{rework_id}/deliver` endpoint
    - Validate status == 'in_progress' (409 otherwise)
    - Validate deliver_photos has at least 1 photo (400 otherwise)
    - Set delivered_at, deliver_remark, status → 'delivered'
    - _Requirements: 4.1, 4.2, 4.4, 4.5_

  - [x] 3.6 Implement `POST /api/processor/rework-orders/{rework_id}/photos` endpoint
    - Accept stage param, file upload
    - Store photo to uploads dir, append path to deliver_photos array
    - Validate tenant ownership
    - _Requirements: 4.3_

  - [x] 3.7 Implement `DELETE /api/processor/rework-orders/{rework_id}/photos` endpoint
    - Remove a photo path from deliver_photos array
    - Validate tenant ownership
    - _Requirements: 4.3_

  - [ ]* 3.8 Write property tests for state machine and tenant isolation
    - **Property 1: State machine enforces valid transitions only**
    - **Property 6: Tenant isolation on rework operations**
    - **Validates: Requirements 1.4, 1.5, 4.5, 9.1, 9.2, 9.3**

  - [ ]* 3.9 Write property test for delivery photo requirement
    - **Property 5: Delivery requires at least one photo**
    - **Validates: Requirements 4.4**

- [x] 4. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Backend — Internal rework endpoints
  - [x] 5.1 Implement `POST /api/internal/qc/rework-orders/{rework_id}/inspect` endpoint in `backend/mvp/routes/qc.py`
    - Validate status == 'delivered' (409 otherwise)
    - Set inspected_at, status → 'inspecting'
    - _Requirements: 5.1, 5.3_

  - [x] 5.2 Implement `POST /api/internal/qc/rework-orders/{rework_id}/complete` endpoint
    - Validate status == 'inspecting' (409 otherwise)
    - Set completed_at, status → 'completed'
    - _Requirements: 5.2, 5.4_

  - [x] 5.3 Implement `POST /api/internal/qc/rework-orders/{rework_id}/receive-material` endpoint
    - Branch A only: update associated MPO status to 'received'
    - Validate rework order has new_po_id set
    - _Requirements: 5.5_

  - [x] 5.4 Update existing `PATCH /api/internal/qc/rework-orders/{rework_id}/status` to support new statuses
    - Extend the `ReworkStatus` model pattern to include all 6 states
    - Add transition validation using `REWORK_TRANSITIONS`
    - _Requirements: 9.1, 9.2, 9.3_

  - [ ]* 5.5 Write property test for timestamp recording
    - **Property 2: State transitions record timestamps**
    - **Validates: Requirements 1.2, 9.4**

- [ ] 6. Backend — Exception confirm logic update
  - [x] 6.1 Modify `confirm_exception()` in `backend/mvp/routes/qc.py` to handle material supplier fault scenarios
    - When `responsible_party='material_supplier'` AND `material_sourcing='processor'`: override to 'processor', create rework_order, set resolution_path='rework_process', record original determination
    - When `responsible_party='material_supplier'` AND `material_sourcing='internal'`: create Material_Redelivery_Request (new table or record), do NOT create rework_order targeting processor
    - Add `material_redelivery_requests` table creation in migration if needed
    - _Requirements: 7.1, 7.2, 7.4, 8.1, 8.2, 8.3, 8.4_

  - [x] 6.2 Implement redelivery completion endpoint `POST /api/internal/qc/redelivery-requests/{id}/receive`
    - Mark redelivery as received, update associated exception status to 'resolved'
    - _Requirements: 7.3_

  - [ ]* 6.3 Write property tests for material supplier fault scenarios
    - **Property 7: Material supplier fault + processor sourcing overrides to processor**
    - **Property 8: Material supplier fault + internal sourcing creates redelivery (no processor rework)**
    - **Property 9: Redelivery completion resolves exception**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4**

  - [ ]* 6.4 Write property test for cost bearer default
    - **Property 10: Cost bearer defaults to processor**
    - **Validates: Requirements 6.2**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Frontend — Processor rework UI
  - [x] 8.1 Modify `backend/static/processor/my-exceptions.html` to add rework action buttons
    - Add "确认返工" button when rework_order status = 'pending'
    - Add "标记交货" button when rework_order status = 'in_progress'
    - Add confirmation dialog with remark input field
    - Wire buttons to call `/api/processor/rework-orders/{id}/confirm` and `/deliver`
    - _Requirements: 1.1, 4.1_

  - [x] 8.2 Create `backend/static/processor/rework-detail.html` page
    - Display rework order details: original order info, material_sourcing_mode, status timeline
    - Photo upload area (reuse outsource_orders photo upload UI pattern)
    - Dynamic action buttons based on current status
    - Cost information display section
    - Link from my-exceptions list to this detail page
    - _Requirements: 1.1, 4.3, 6.1_

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- The migration (task 1) must run before any backend tasks
- The rework_service (task 2) must be complete before processor endpoints (task 3) since confirm calls it
- Frontend (task 8) depends on all backend endpoints being available
