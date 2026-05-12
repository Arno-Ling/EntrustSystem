# Requirements Document

## Introduction

将"材料供应方式"（我方统一供料 vs 加工方自采）的决策时间点从中标后（outsource_orders 阶段）前移到询价发送前（outsource_requests 阶段）。当内部用户点击"群发"按钮时，系统根据所选供料模式同步执行：向加工方发送委外询价邀请，并在"我方供料"模式下自动创建多行材料询价单并群发给材料方。加工方在"加工方自采"模式下报价时需按委外项填写材料单价。

## Glossary

- **System**: 模具委外采购系统后端服务
- **Internal_User**: 我方内部操作人员（采购经理/alice 等），tenant_type='internal'
- **Processor**: 加工方租户，接收委外询价邀请并提交报价
- **Material_Supplier**: 材料方租户，接收材料询价邀请并提交材料报价
- **Outsource_Request**: 委外询价单，状态流：draft → inviting → pending_award → awarded
- **Material_Inquiry**: 材料询价单，包含一或多行材料明细，发给材料方报价
- **Material_Inquiry_Line**: 材料询价行，一条材料询价单中的单行材料（材料号+规格+数量）
- **Broadcast_Action**: 群发操作，Internal_User 点击"群发"按钮触发的一次性动作
- **Material_Sourcing_Mode**: 材料供应方式，取值 'internal'（我方统一供料）或 'processor'（加工方自采）
- **Scope_Item**: 委外项，outsource_scope_items 表中的一行，代表一个粒度的委外工作内容
- **Outsource_Quotation_Line**: 加工方逐项报价行，对应一个 Scope_Item 的报价明细

## Requirements

### Requirement 1: 询价单级别材料供应方式字段

**User Story:** As an Internal_User, I want to set the material sourcing mode on the outsource request (before sending), so that the decision is made upfront and drives downstream behavior.

#### Acceptance Criteria

1. THE System SHALL provide a `material_sourcing` column on the `outsource_requests` table with allowed values 'internal' and 'processor'
2. WHEN an Outsource_Request is in 'draft' status, THE System SHALL allow Internal_User to set or update the `material_sourcing` value
3. WHEN an Outsource_Request has `material_sourcing` set to NULL, THE System SHALL prevent the Broadcast_Action from executing
4. THE System SHALL store the `material_sourcing` value as a VARCHAR(16) column that is nullable (NULL means not yet decided)

### Requirement 2: 多行材料询价单数据模型

**User Story:** As an Internal_User, I want a single material inquiry to contain multiple material lines, so that material suppliers can quote all needed materials at once.

#### Acceptance Criteria

1. THE System SHALL provide a `material_inquiry_lines` table with columns: id, inquiry_id (FK to material_inquiries), material_code, material_name, spec, qty, unit, and sort_order
2. THE System SHALL allow one Material_Inquiry to reference multiple Material_Inquiry_Line records
3. THE System SHALL require at least one Material_Inquiry_Line per Material_Inquiry before the inquiry can be sent
4. WHEN a Material_Inquiry is created during Broadcast_Action, THE System SHALL auto-populate Material_Inquiry_Line records from the project's bill of materials (project_parts.material column)

### Requirement 3: 材料询价单关联前移到询价单级别

**User Story:** As an Internal_User, I want material inquiries to be linked to the outsource request (not the order), so that material sourcing can happen before a processor wins the bid.

#### Acceptance Criteria

1. THE System SHALL add an `outsource_request_id` column (FK to outsource_requests) to the `material_inquiries` table
2. THE System SHALL make the existing `outsource_order_id` column on `material_inquiries` nullable
3. WHEN a Material_Inquiry is created during Broadcast_Action, THE System SHALL set `outsource_request_id` to the current Outsource_Request id and leave `outsource_order_id` as NULL
4. WHEN a Processor wins the bid and an outsource_order is created, THE System SHALL update the pre-existing Material_Inquiry's `outsource_order_id` to reference the new order

### Requirement 4: 群发同步触发材料询价（我方供料模式）

**User Story:** As an Internal_User, I want the broadcast action to simultaneously send processor invitations AND material inquiries when material_sourcing='internal', so that I only need one click to initiate both sourcing tracks.

#### Acceptance Criteria

1. WHEN Internal_User triggers Broadcast_Action AND `material_sourcing` is 'internal', THE System SHALL create one Material_Inquiry record linked to the Outsource_Request
2. WHEN Internal_User triggers Broadcast_Action AND `material_sourcing` is 'internal', THE System SHALL create Material_Inquiry_Line records by extracting distinct (material_code, spec) combinations from the project's parts list
3. WHEN Internal_User triggers Broadcast_Action AND `material_sourcing` is 'internal', THE System SHALL create material_inquiry_invitations for all material-type tenants that have a linked supplier
4. WHEN Internal_User triggers Broadcast_Action AND `material_sourcing` is 'internal', THE System SHALL set the Material_Inquiry status to 'inviting' and record `sent_at` timestamps on all invitations
5. WHEN Internal_User triggers Broadcast_Action AND `material_sourcing` is 'processor', THE System SHALL only create processor invitations without creating any Material_Inquiry
6. THE System SHALL execute processor invitation creation and material inquiry creation within a single database transaction
7. THE System SHALL return the count of invited processors and invited material suppliers in the Broadcast_Action response

### Requirement 5: 加工方询价详情页显示供料模式提示

**User Story:** As a Processor, I want to see a clear notice about material sourcing mode on the invitation detail page, so that I know whether materials will be supplied to me or I need to source them myself.

#### Acceptance Criteria

1. WHEN a Processor views an invitation detail AND the associated Outsource_Request has `material_sourcing` = 'internal', THE System SHALL include a `material_sourcing` field with value 'internal' in the invitation detail API response
2. WHEN a Processor views an invitation detail AND the associated Outsource_Request has `material_sourcing` = 'processor', THE System SHALL include a `material_sourcing` field with value 'processor' in the invitation detail API response
3. WHEN `material_sourcing` is 'internal', THE invitation detail page SHALL display a notice: "材料由我方统一供应，无需报材料费"
4. WHEN `material_sourcing` is 'processor', THE invitation detail page SHALL display a notice: "材料由贵方自行采购，请在报价中包含材料成本"

### Requirement 6: 加工方报价包含材料单价（加工方自采模式）

**User Story:** As a Processor, I want to fill in material cost per scope item when I self-source materials, so that the internal team can see the material cost breakdown.

#### Acceptance Criteria

1. THE System SHALL add a `material_unit_price` column (DECIMAL(14,2), nullable) to the `outsource_quotation_lines` table
2. WHEN `material_sourcing` is 'processor' AND the quote form uses line-by-line mode, THE System SHALL accept a `material_unit_price` field per Outsource_Quotation_Line
3. WHEN `material_sourcing` is 'processor', THE System SHALL require `material_unit_price` to be provided (> 0) for each Outsource_Quotation_Line
4. WHEN `material_sourcing` is 'internal', THE System SHALL ignore any `material_unit_price` values submitted and store them as NULL
5. THE System SHALL include `material_unit_price` in the quotation line detail API response when the value is not NULL

### Requirement 7: 材料方对多行询价逐行报价

**User Story:** As a Material_Supplier, I want to quote a unit price per material line in a multi-line inquiry, so that I can provide pricing for each material separately.

#### Acceptance Criteria

1. THE System SHALL add an `inquiry_line_id` column (FK to material_inquiry_lines, nullable) to the `material_quotations` table
2. WHEN a Material_Inquiry has multiple lines, THE System SHALL allow Material_Supplier to submit one quotation per Material_Inquiry_Line
3. WHEN a Material_Supplier submits a quote for a multi-line inquiry, THE System SHALL validate that each quoted line references a valid Material_Inquiry_Line belonging to that inquiry
4. THE System SHALL aggregate line-level quotations into a total price for comparison during the award process
5. IF a Material_Supplier submits a quote for a line that already has a quotation from the same supplier, THEN THE System SHALL update the existing quotation rather than creating a duplicate

### Requirement 8: 中标后关联材料询价到加工单

**User Story:** As an Internal_User, I want the system to automatically link pre-existing material inquiries to the new outsource order after a processor wins the bid, so that material tracking is continuous from inquiry through delivery.

#### Acceptance Criteria

1. WHEN an outsource_order is created from an awarded Outsource_Request, THE System SHALL find all Material_Inquiry records where `outsource_request_id` matches the awarded request
2. WHEN an outsource_order is created from an awarded Outsource_Request, THE System SHALL update each found Material_Inquiry's `outsource_order_id` to the newly created order id
3. WHEN an outsource_order is created, THE System SHALL set `outsource_orders.material_sourcing` to the value from the parent Outsource_Request's `material_sourcing` column
4. IF no Material_Inquiry exists for the Outsource_Request at award time, THEN THE System SHALL proceed with order creation without error

### Requirement 9: 前端询价单编辑页供料模式选择

**User Story:** As an Internal_User, I want to select the material sourcing mode on the outsource request edit page before broadcasting, so that I can make the decision as part of the inquiry preparation workflow.

#### Acceptance Criteria

1. WHILE an Outsource_Request is in 'draft' status, THE outsource request edit page SHALL display a radio button group for Material_Sourcing_Mode selection with options "我方统一供料 (internal)" and "加工方自采 (processor)"
2. WHEN Internal_User selects a Material_Sourcing_Mode and saves, THE System SHALL persist the selection to `outsource_requests.material_sourcing`
3. WHEN Internal_User clicks the broadcast button without selecting a Material_Sourcing_Mode, THE System SHALL display an error message: "请先选择材料供应方式"
4. WHILE an Outsource_Request is in 'inviting' or later status, THE outsource request page SHALL display the Material_Sourcing_Mode as read-only text

### Requirement 10: 群发响应包含材料询价结果

**User Story:** As an Internal_User, I want the broadcast response to confirm both processor invitations and material inquiry creation, so that I have immediate feedback on what was triggered.

#### Acceptance Criteria

1. WHEN Broadcast_Action completes successfully with `material_sourcing` = 'internal', THE System SHALL return a response containing: `invited_processor_count`, `material_inquiry_id`, `material_inquiry_no`, and `invited_material_supplier_count`
2. WHEN Broadcast_Action completes successfully with `material_sourcing` = 'processor', THE System SHALL return a response containing: `invited_processor_count` and `material_inquiry_id` as NULL
3. IF Broadcast_Action fails during material inquiry creation (after processor invitations are already created), THEN THE System SHALL rollback all changes including processor invitations
