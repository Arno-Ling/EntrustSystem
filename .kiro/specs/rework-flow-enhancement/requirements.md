# Requirements Document

## Introduction

本文档定义返工流程增强（Rework Flow Enhancement）的功能需求。该功能扩展现有质检异常处理系统，使加工方能够主动参与返工流程（确认返工、标记交货），系统根据材料供应模式（material_sourcing）自动分支处理，并增加费用追踪能力。同时处理材料方责任场景下的两种不同流转路径。

## Glossary

- **Rework_System**: 返工流程管理系统，负责返工单的创建、状态流转、分支处理和费用追踪
- **Processor**: 加工方（外协供应商），负责确认返工并重新加工交货
- **Internal_User**: 内部人员（我方），负责质检、补料收货确认、返工完成确认
- **Material_Supplier**: 材料供应商，负责提供原材料
- **Rework_Order**: 返工单，记录一次返工任务的全生命周期
- **Material_Purchase_Order (MPO)**: 材料采购单，用于向材料方采购原材料
- **Material_Sourcing**: 材料供应模式，'internal' 表示我方找的材料方，'processor' 表示加工方找的材料方
- **Material_Redelivery_Request**: 材料补发请求，材料方责任且我方找的材料方时，要求材料方重新发货的请求
- **QC_Inspection**: 质量检验，对返工后的产品进行质量验收

## Requirements

### Requirement 1: Processor Confirms Rework (加工方确认返工)

**User Story:** As a processor, I want to confirm a rework order assigned to me, so that the rework process can officially begin and the system knows I have acknowledged the task.

#### Acceptance Criteria

1. WHEN a processor receives a rework order with status 'pending', THE Rework_System SHALL display the rework order in the processor's rework order list with a "confirm" action available
2. WHEN a processor submits a confirmation for a rework order, THE Rework_System SHALL transition the rework order status from 'pending' to 'confirmed' and record the confirmation timestamp and confirming user
3. WHEN a processor submits a confirmation, THE Rework_System SHALL accept an optional remark field and persist it on the rework order
4. IF a processor attempts to confirm a rework order that is not in 'pending' status, THEN THE Rework_System SHALL reject the request with a 409 status code and a message indicating the current status
5. IF a processor attempts to confirm a rework order that does not belong to their tenant, THEN THE Rework_System SHALL reject the request with a 403 status code

### Requirement 2: Branch A — Auto-Create Material PO (我方补料分支)

**User Story:** As an internal user, I want the system to automatically create a material purchase order when the processor confirms a rework order with internal material sourcing, so that the material re-procurement process starts without manual intervention.

#### Acceptance Criteria

1. WHEN a rework order is confirmed AND the original outsource order has material_sourcing = 'internal', THE Rework_System SHALL automatically create a new Material_Purchase_Order copying material_code, spec, qty, supplier_id, and unit from the original MPO
2. WHEN the auto-created MPO is generated, THE Rework_System SHALL set the MPO remark to reference the rework order number (e.g., '异常返工补料 - RW-xxx')
3. WHEN the auto-created MPO is generated, THE Rework_System SHALL link the new MPO id to the rework order's new_po_id field
4. WHEN the auto-created MPO is generated, THE Rework_System SHALL transition the rework order status from 'confirmed' to 'in_progress'
5. WHEN the auto-created MPO is generated, THE Rework_System SHALL record the material_sourcing_mode as 'internal' on the rework order
6. IF the system cannot find an original MPO to copy from, THEN THE Rework_System SHALL leave the rework order in 'confirmed' status and flag it for manual intervention

### Requirement 3: Branch B — Processor Self-Sources (加工方自采分支)

**User Story:** As a processor with my own material supplier, I want the system to let me proceed directly to rework after confirmation without waiting for material delivery, so that I can source materials myself and complete the rework faster.

#### Acceptance Criteria

1. WHEN a rework order is confirmed AND the original outsource order has material_sourcing = 'processor', THE Rework_System SHALL transition the rework order status directly from 'confirmed' to 'in_progress' without creating a Material_Purchase_Order
2. WHEN Branch B is activated, THE Rework_System SHALL record the material_sourcing_mode as 'processor' on the rework order
3. WHEN Branch B is activated, THE Rework_System SHALL NOT create any Material_Purchase_Order

### Requirement 4: Processor Delivers Rework (加工方交货)

**User Story:** As a processor, I want to mark a rework order as delivered and upload proof photos, so that the internal team knows the reworked parts are ready for inspection.

#### Acceptance Criteria

1. WHEN a processor submits a delivery for a rework order in 'in_progress' status, THE Rework_System SHALL transition the status to 'delivered' and record the delivery timestamp
2. WHEN a processor submits a delivery, THE Rework_System SHALL accept an optional delivery remark and persist it on the rework order
3. WHEN a processor uploads photos for a rework order, THE Rework_System SHALL store the photo paths in the deliver_photos JSON array field
4. IF a processor attempts to deliver a rework order without at least 1 uploaded photo, THEN THE Rework_System SHALL reject the request with a 400 status code indicating photos are required
5. IF a processor attempts to deliver a rework order that is not in 'in_progress' status, THEN THE Rework_System SHALL reject the request with a 409 status code

### Requirement 5: Internal Inspects and Completes Rework (内部质检与完成)

**User Story:** As an internal user, I want to trigger quality inspection on delivered rework and mark it complete when it passes, so that the rework lifecycle is properly closed.

#### Acceptance Criteria

1. WHEN an internal user triggers inspection on a rework order in 'delivered' status, THE Rework_System SHALL transition the status to 'inspecting' and record the inspection start timestamp
2. WHEN an internal user marks a rework order as complete after inspection passes, THE Rework_System SHALL transition the status from 'inspecting' to 'completed' and record the completion timestamp
3. IF an internal user attempts to trigger inspection on a rework order not in 'delivered' status, THEN THE Rework_System SHALL reject the request with a 409 status code
4. IF an internal user attempts to complete a rework order not in 'inspecting' status, THEN THE Rework_System SHALL reject the request with a 409 status code
5. WHEN an internal user confirms material receipt for a Branch A rework order, THE Rework_System SHALL update the associated MPO status to 'received'

### Requirement 6: Cost Tracking (费用追踪)

**User Story:** As an internal user, I want to record cost information on rework orders, so that we can track financial impact of quality exceptions and charge the responsible party.

#### Acceptance Criteria

1. THE Rework_System SHALL store cost_bearer, cost_amount, and cost_remark fields on each rework order
2. WHEN a rework order is created with responsible_party = 'processor', THE Rework_System SHALL default cost_bearer to 'processor'
3. WHEN an internal user updates cost information on a rework order, THE Rework_System SHALL persist the cost_amount (decimal, >= 0) and cost_remark
4. THE Rework_System SHALL restrict cost field modifications to internal users only

### Requirement 7: Material Supplier Fault with Internal Sourcing (材料方责任 — 我方找的材料方)

**User Story:** As an internal user, when a quality exception is the material supplier's fault and we sourced the material ourselves, I want the system to create a simple re-delivery request to the material supplier, so that the supplier re-ships materials and we receive them without involving the processor in a rework flow.

#### Acceptance Criteria

1. WHEN an exception is confirmed with responsible_party = 'material_supplier' AND the material_sourcing = 'internal', THE Rework_System SHALL create a Material_Redelivery_Request targeting the original material supplier
2. WHEN a Material_Redelivery_Request is created, THE Rework_System SHALL NOT create a rework_order targeting the processor
3. WHEN the material supplier re-ships and internal user confirms receipt, THE Rework_System SHALL mark the redelivery as complete and update the exception status to 'resolved'
4. WHEN a Material_Redelivery_Request is created, THE Rework_System SHALL reference the original Material_Purchase_Order and the quality exception

### Requirement 8: Material Supplier Fault with Processor Sourcing (材料方责任 — 加工方找的材料方)

**User Story:** As an internal user, when a quality exception is the material supplier's fault but the processor chose the material supplier, I want the system to push responsibility to the processor, so that the processor bears the consequence of their supplier choice and follows the standard processor rework path.

#### Acceptance Criteria

1. WHEN an exception is confirmed with responsible_party = 'material_supplier' AND the material_sourcing = 'processor', THE Rework_System SHALL override the responsible_party to 'processor'
2. WHEN the responsibility is overridden to 'processor', THE Rework_System SHALL create a rework_order targeting the processor following the standard processor rework flow
3. WHEN the responsibility is overridden, THE Rework_System SHALL record the original responsible_party determination ('material_supplier') and the override reason in the exception responsibility record
4. WHEN the responsibility is overridden to 'processor', THE Rework_System SHALL set the resolution_path to 'rework_process'

### Requirement 9: Rework Order State Machine (返工单状态机)

**User Story:** As a system operator, I want the rework order to follow a strict state machine, so that invalid state transitions are prevented and the workflow integrity is maintained.

#### Acceptance Criteria

1. THE Rework_System SHALL enforce the following valid state transitions: pending→confirmed, confirmed→in_progress, in_progress→delivered, delivered→inspecting, inspecting→completed
2. THE Rework_System SHALL allow cancellation (transition to 'cancelled') from any status except 'completed' and 'cancelled'
3. IF any API request attempts an invalid state transition, THEN THE Rework_System SHALL reject the request with a 409 status code indicating the current status and allowed transitions
4. THE Rework_System SHALL record timestamps for each state transition (confirmed_at, delivered_at, inspected_at, completed_at)
