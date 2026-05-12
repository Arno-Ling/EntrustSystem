"""
返工分支服务 — 加工方确认返工后的自动分支处理。

Branch A (material_sourcing='internal'): 自动创建补料采购单
Branch B (material_sourcing='processor'): 加工方自采，直接进入重工
"""
from __future__ import annotations

import uuid
from datetime import datetime


def process_rework_confirmation(rework_order_id: int, cur) -> dict:
    """
    加工方确认后自动分支处理。

    1. 查询原单的 material_sourcing
    2. Branch A: 创建补料 MPO
    3. Branch B: 无额外操作
    4. 设置 material_sourcing_mode, cost_bearer, status → in_progress

    Args:
        rework_order_id: 返工单 ID
        cur: 数据库游标（在事务内）

    Returns:
        {"branch": "A"|"B", "new_po_id": int|None, "material_sourcing_mode": str}
    """
    # 查询返工单 + 原加工单
    cur.execute(
        """
        SELECT rw.id, rw.rework_no, rw.project_id, rw.original_order_id, rw.original_po_id,
               o.material_sourcing, o.request_id
        FROM rework_orders rw
        LEFT JOIN outsource_orders o ON rw.original_order_id = o.id
        WHERE rw.id = %s
        """,
        (rework_order_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"branch": "B", "new_po_id": None, "material_sourcing_mode": None}

    rw_id, rw_no, project_id, original_order_id, original_po_id, \
        material_sourcing, request_id = row

    # Fallback: 从 outsource_requests 获取 material_sourcing
    if not material_sourcing and request_id:
        cur.execute(
            "SELECT material_sourcing FROM outsource_requests WHERE id = %s",
            (request_id,),
        )
        req_row = cur.fetchone()
        if req_row:
            material_sourcing = req_row[0]

    sourcing_mode = material_sourcing or "processor"  # 默认 processor

    # 设置 material_sourcing_mode + cost_bearer
    cur.execute(
        """
        UPDATE rework_orders
        SET material_sourcing_mode = %s, cost_bearer = 'processor'
        WHERE id = %s
        """,
        (sourcing_mode, rework_order_id),
    )

    new_po_id = None
    branch = "B"

    if sourcing_mode == "internal":
        # Branch A: 自动创建补料单
        new_po_id = _create_rework_material_po(
            rework_order_id, rw_no, project_id, original_po_id, cur
        )
        if new_po_id:
            cur.execute(
                "UPDATE rework_orders SET new_po_id = %s WHERE id = %s",
                (new_po_id, rework_order_id),
            )
            branch = "A"
        # 如果找不到原 MPO，仍然推进到 in_progress（人工后续处理）

    # 推进到 in_progress
    cur.execute(
        "UPDATE rework_orders SET status = 'in_progress' WHERE id = %s",
        (rework_order_id,),
    )

    return {
        "branch": branch,
        "new_po_id": new_po_id,
        "material_sourcing_mode": sourcing_mode,
    }


def _create_rework_material_po(
    rework_order_id: int,
    rework_no: str,
    project_id: int,
    original_po_id: int | None,
    cur,
) -> int | None:
    """
    为 Branch A 创建补料采购单。

    复制原 MPO 的 material_code, spec, qty, supplier_id, unit, unit_price。
    如果找不到原 MPO，返回 None。
    """
    # 查找原始 MPO
    original_mpo = None
    if original_po_id:
        cur.execute(
            """
            SELECT id, supplier_id, tenant_id, material_code, spec, qty, unit, unit_price
            FROM material_purchase_orders WHERE id = %s
            """,
            (original_po_id,),
        )
        original_mpo = cur.fetchone()

    if not original_mpo:
        # Fallback: 找同项目最近的 MPO
        cur.execute(
            """
            SELECT id, supplier_id, tenant_id, material_code, spec, qty, unit, unit_price
            FROM material_purchase_orders
            WHERE project_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (project_id,),
        )
        original_mpo = cur.fetchone()

    if not original_mpo:
        return None  # 找不到原 MPO，需人工处理

    _, supplier_id, tenant_id, material_code, spec, qty, unit, unit_price = original_mpo

    # 生成 PO 编号
    po_no = f"MP-RW-{uuid.uuid4().hex[:6].upper()}"

    cur.execute(
        """
        INSERT INTO material_purchase_orders
            (po_no, project_id, supplier_id, tenant_id,
             material_code, spec, qty, unit, unit_price,
             status, remark, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'drafted', %s, NULL)
        RETURNING id
        """,
        (po_no, project_id, supplier_id, tenant_id,
         material_code, spec, qty or 1, unit or "kg", unit_price,
         f"异常返工补料 - {rework_no}"),
    )
    return cur.fetchone()[0]
