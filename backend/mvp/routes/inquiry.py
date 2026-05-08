"""
批次 2-A：材料询价（我方找材料方模式）

流程：
  1. 采购经理在外协中标审批页选 "我方找材料方" → outsource_orders.material_sourcing='internal'
  2. alice 进【材料询价】页，新建 material_inquiries（基于 outsource_order_id）
  3. 【群发询价】→ 为所有能供该材料的材料方建 invitations
  4. 材料方端在【材料询价邀请】tab 看邀请，逐单报价
  5. alice【截止报价】→ 推送审批任务给采购经理
  6. 采购经理选中标家 → 生成 material_purchase_orders（与现有流程打通）

API（我方 internal）：
  POST   /api/internal/outsource-orders/{id}/sourcing     决定材料供应方式
  GET    /api/internal/material-inquiries                 列表
  POST   /api/internal/material-inquiries                 新建
  GET    /api/internal/material-inquiries/{id}            详情
  GET    /api/internal/material-inquiries/{id}/candidates 候选材料方
  POST   /api/internal/material-inquiries/{id}/send       群发
  POST   /api/internal/material-inquiries/{id}/close-quoting  截止 → 创建审批
  POST   /api/internal/approvals/{task_id}/award-material    采购经理选中标

API（材料方 material）：
  GET    /api/material/inquiries                          我的询价邀请
  GET    /api/material/inquiries/{inv_id}                 详情
  POST   /api/material/inquiries/{inv_id}/quote           报价

API（加工方 processor）：
  POST   /api/processor/orders/{id}/proofs                上传自采凭证
  GET    /api/processor/orders/{id}/proofs                列出自采凭证
  DELETE /api/processor/orders/{id}/proofs/{proof_id}     删一条
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type


# =============================================================================
# Routers
# =============================================================================

internal_router  = APIRouter(prefix="/api/internal",  tags=["material-inquiry-internal"])
material_router  = APIRouter(prefix="/api/material",  tags=["material-inquiry-material"])
processor_router = APIRouter(prefix="/api/processor", tags=["processor-proofs"])

_require_internal  = require_tenant_type("internal")
_require_material  = require_tenant_type("material")
_require_processor = require_tenant_type("processor")


# =============================================================================
# Pydantic
# =============================================================================

class SourcingDecision(BaseModel):
    material_sourcing: str = Field(..., pattern="^(internal|processor)$")


class InquiryCreate(BaseModel):
    outsource_order_id: int
    title: Optional[str] = None
    material_code: str = Field(..., max_length=64)
    material_name: Optional[str] = None
    spec: Optional[str] = None
    qty: float = Field(..., gt=0)
    unit: str = "kg"
    required_date: Optional[str] = None


class InquiryQuoteSubmit(BaseModel):
    unit_price: float = Field(..., gt=0)
    lead_time_days: int = Field(..., ge=1)
    note: Optional[str] = None


class AwardMaterial(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = None
    awarded_invitation_id: Optional[int] = None


# =============================================================================
# 1. 决定材料供应方式
# =============================================================================

@internal_router.post("/outsource-orders/{order_id}/sourcing")
async def decide_material_sourcing(
    order_id: int,
    payload: SourcingDecision,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        """
        SELECT o.id, o.status, o.material_sourcing, p.tenant_id
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE o.id = %s
        """,
        (order_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if row["status"] not in ("awarded",):
        raise HTTPException(status_code=409,
                            detail=f"订单状态 '{row['status']}' 不允许设置材料供应方式")
    if row["material_sourcing"]:
        raise HTTPException(status_code=409,
                            detail=f"已决定为 '{row['material_sourcing']}'，不可重复")

    db.execute(
        """
        UPDATE outsource_orders
        SET material_sourcing = %s,
            material_sourcing_decided_by = %s,
            material_sourcing_decided_at = %s
        WHERE id = %s
        """,
        (payload.material_sourcing, user.user_id, datetime.utcnow(), order_id),
    )
    return {"material_sourcing": payload.material_sourcing}


# =============================================================================
# 2. 材料询价单（我方视角）
# =============================================================================

def _gen_inquiry_no(project_id: int) -> str:
    row = db.fetch_one("SELECT project_no FROM projects WHERE id=%s", (project_id,))
    pn = row["project_no"] if row else f"P{project_id}"
    return f"MI-{pn}-{uuid.uuid4().hex[:4].upper()}"


@internal_router.get("/material-inquiries")
async def list_inquiries(
    status_filter: Optional[str] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if status_filter:
        where += " AND mi.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT mi.*, p.project_no, p.name AS project_name,
               o.order_no AS outsource_order_no
        FROM material_inquiries mi
        JOIN projects p ON mi.project_id = p.id
        JOIN outsource_orders o ON mi.outsource_order_id = o.id
        {where}
        ORDER BY mi.created_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        if r.get("qty") is not None:
            r["qty"] = float(r["qty"])
    return {"items": rows, "total": len(rows)}


@internal_router.post("/material-inquiries", status_code=201)
async def create_inquiry(
    payload: InquiryCreate,
    user: CurrentUser = Depends(_require_internal),
):
    # 校验 outsource_order 归属且已设为 internal 模式
    oo = db.fetch_one(
        """
        SELECT o.id, o.material_sourcing, p.tenant_id, p.id AS project_id,
               p.project_no, p.name AS project_name
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE o.id = %s
        """,
        (payload.outsource_order_id,),
    )
    if oo is None or oo["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if oo["material_sourcing"] != "internal":
        raise HTTPException(status_code=409,
                            detail="只有 material_sourcing='internal' 的加工单可发材料询价")

    inquiry_no = _gen_inquiry_no(oo["project_id"])
    title = payload.title or f"{oo['project_no']} 材料 {payload.material_code}"

    new_id = db.execute(
        """
        INSERT INTO material_inquiries
            (inquiry_no, outsource_order_id, project_id, title,
             material_code, material_name, spec, qty, unit,
             required_date, status, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
        RETURNING id
        """,
        (inquiry_no, payload.outsource_order_id, oo["project_id"], title,
         payload.material_code, payload.material_name, payload.spec,
         payload.qty, payload.unit, payload.required_date, user.user_id),
    )
    return {"id": new_id, "inquiry_no": inquiry_no, "status": "draft"}


@internal_router.get("/material-inquiries/{inq_id}")
async def get_inquiry(inq_id: int, user: CurrentUser = Depends(_require_internal)):
    mi = db.fetch_one(
        """
        SELECT mi.*, p.project_no, p.name AS project_name, p.tenant_id,
               o.order_no AS outsource_order_no
        FROM material_inquiries mi
        JOIN projects p ON mi.project_id = p.id
        JOIN outsource_orders o ON mi.outsource_order_id = o.id
        WHERE mi.id = %s
        """,
        (inq_id,),
    )
    if mi is None or mi["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if mi.get("qty") is not None:
        mi["qty"] = float(mi["qty"])

    invs = db.fetch_all(
        """
        SELECT i.id AS invitation_id, i.supplier_id, i.invitation_status,
               i.sent_at, i.quoted_at,
               s.name AS supplier_name, s.category,
               q.id AS quotation_id, q.unit_price, q.lead_time_days,
               q.note, q.submitted_at
        FROM material_inquiry_invitations i
        JOIN suppliers s ON i.supplier_id = s.id
        LEFT JOIN material_quotations q ON q.invitation_id = i.id
        WHERE i.inquiry_id = %s
        ORDER BY i.sent_at ASC
        """,
        (inq_id,),
    )
    for v in invs:
        if v.get("unit_price") is not None:
            v["unit_price"] = float(v["unit_price"])

    stats = {
        "total_invited": len(invs),
        "quoted":        sum(1 for v in invs if v["invitation_status"] == "quoted"),
        "pending":       sum(1 for v in invs if v["invitation_status"] == "sent"),
        "no_response":   sum(1 for v in invs if v["invitation_status"] == "no_response"),
    }
    return {"inquiry": mi, "invitations": invs, "stats": stats}


@internal_router.get("/material-inquiries/{inq_id}/candidates")
async def list_inquiry_candidates(
    inq_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """候选材料方：凡是在 material_prices 里登记过该材料的 supplier 都算。
    MVP 简化：所有 material 类型 tenant 关联的 supplier 都列出来。"""
    mi = db.fetch_one(
        """
        SELECT mi.id, mi.material_code, p.tenant_id
        FROM material_inquiries mi
        JOIN projects p ON mi.project_id = p.id
        WHERE mi.id = %s
        """,
        (inq_id,),
    )
    if mi is None or mi["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    rows = db.fetch_all(
        """
        SELECT s.id, s.name, s.category, s.contact_name, s.contact_phone,
               (SELECT COUNT(*) FROM material_prices mp
                  WHERE mp.supplier_id = s.id AND mp.material_code = %s) AS has_price,
               EXISTS(SELECT 1 FROM material_inquiry_invitations i
                      WHERE i.inquiry_id = %s AND i.supplier_id = s.id) AS already_invited
        FROM suppliers s
        WHERE s.id IN (SELECT supplier_id FROM tenants WHERE tenant_type='material' AND supplier_id IS NOT NULL)
        ORDER BY has_price DESC, s.name
        """,
        (mi["material_code"], inq_id),
    )
    return {"items": rows, "total": len(rows)}


@internal_router.post("/material-inquiries/{inq_id}/send")
async def broadcast_inquiry(
    inq_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    mi = db.fetch_one(
        """
        SELECT mi.id, mi.status, mi.material_code, p.tenant_id
        FROM material_inquiries mi
        JOIN projects p ON mi.project_id = p.id
        WHERE mi.id = %s
        """,
        (inq_id,),
    )
    if mi is None or mi["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if mi["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"只有 draft 询价单可群发；当前 '{mi['status']}'")

    # 材料方租户列表（有 supplier_id 关联的）
    tenants = db.fetch_all(
        """
        SELECT t.id AS tenant_id, t.supplier_id
        FROM tenants t
        WHERE t.tenant_type='material' AND t.supplier_id IS NOT NULL
        """
    )
    if not tenants:
        raise HTTPException(status_code=400, detail="没有可邀请的材料方")

    now = datetime.utcnow()
    inserted = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for t in tenants:
                try:
                    cur.execute(
                        """
                        INSERT INTO material_inquiry_invitations
                            (inquiry_id, supplier_id, tenant_id, invitation_status, sent_at)
                        VALUES (%s, %s, %s, 'sent', %s)
                        ON CONFLICT (inquiry_id, supplier_id) DO NOTHING
                        """,
                        (inq_id, t["supplier_id"], t["tenant_id"], now),
                    )
                    if cur.rowcount:
                        inserted += 1
                except Exception:
                    pass
            cur.execute(
                "UPDATE material_inquiries SET status='inviting' WHERE id=%s",
                (inq_id,),
            )

    return {"invited_count": inserted, "status": "inviting"}


@internal_router.post("/material-inquiries/{inq_id}/close-quoting")
async def close_inquiry_quoting(
    inq_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    mi = db.fetch_one(
        """
        SELECT mi.*, p.tenant_id, p.project_no
        FROM material_inquiries mi
        JOIN projects p ON mi.project_id = p.id
        WHERE mi.id = %s
        """,
        (inq_id,),
    )
    if mi is None or mi["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if mi["status"] != "inviting":
        raise HTTPException(status_code=409, detail=f"只有 inviting 状态可截止；当前 '{mi['status']}'")

    quoted = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM material_inquiry_invitations
        WHERE inquiry_id = %s AND invitation_status = 'quoted'
        """,
        (inq_id,),
    )
    if not quoted or quoted["c"] == 0:
        raise HTTPException(status_code=400, detail="没有任何材料方回复报价，无法截止")

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE material_inquiry_invitations SET invitation_status='no_response' "
                "WHERE inquiry_id=%s AND invitation_status='sent'",
                (inq_id,),
            )

            # 创建审批任务
            fake_inst = uuid.uuid5(uuid.NAMESPACE_URL, f"material_inquiry:{inq_id}")
            node_id = f"material_award:{inq_id}"
            cur.execute(
                "SELECT id FROM workflow_approval_tasks WHERE node_id=%s AND status='pending'",
                (node_id,),
            )
            existing = cur.fetchone()
            if existing:
                task_id = existing[0]
            else:
                cur.execute(
                    """
                    INSERT INTO workflow_approval_tasks
                        (instance_id, node_id, assignee_type, assignee_id, status)
                    VALUES (%s, %s, 'role', 'ADMIN', 'pending')
                    RETURNING id
                    """,
                    (str(fake_inst), node_id),
                )
                task_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO workflow_approval_records
                        (id, instance_id, node_id, action, actor_id,
                         assignee_type, assignee_id, comment, metadata_json, created_at)
                    VALUES (%s, %s, %s, 'submit', %s, 'role', 'ADMIN', %s, %s, %s)
                    """,
                    (str(uuid.uuid4()), str(fake_inst), node_id, str(user.user_id),
                     f"材料询价截止，请选择中标: inquiry_id={inq_id}",
                     json.dumps({"kind":"material_award","inquiry_id":inq_id,
                                 "task_id":task_id}, ensure_ascii=False),
                     datetime.utcnow()),
                )

            cur.execute(
                "UPDATE material_inquiries SET status='pending_award', approval_task_id=%s, closed_at=%s WHERE id=%s",
                (task_id, datetime.utcnow(), inq_id),
            )

    return {"task_id": task_id, "status": "pending_award"}


# =============================================================================
# 3. 采购经理审批材料中标
# =============================================================================

@internal_router.post("/approvals/{task_id}/award-material")
async def award_material(
    task_id: int,
    payload: AwardMaterial,
    user: CurrentUser = Depends(_require_internal),
):
    task = db.fetch_one(
        """
        SELECT id, instance_id::text AS instance_hex, node_id, status
        FROM workflow_approval_tasks WHERE id=%s
        """,
        (task_id,),
    )
    if task is None:
        raise HTTPException(status_code=404)
    if task["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Task already {task['status']}")
    node = task["node_id"] or ""
    if not node.startswith("material_award:"):
        raise HTTPException(status_code=400, detail="非 material_award 任务")
    inq_id = int(node.split(":",1)[1])

    awarded_data = None
    if payload.action == "approve":
        if not payload.awarded_invitation_id:
            raise HTTPException(status_code=400, detail="approve 必须指定 awarded_invitation_id")
        awarded_data = db.fetch_one(
            """
            SELECT i.id AS invitation_id, i.supplier_id, i.tenant_id AS material_tenant_id,
                   q.id AS quotation_id, q.unit_price, q.lead_time_days,
                   mi.outsource_order_id, mi.project_id, mi.material_code, mi.spec,
                   mi.qty, mi.unit, mi.required_date, mi.inquiry_no,
                   p.project_no
            FROM material_inquiry_invitations i
            JOIN material_quotations q ON q.invitation_id = i.id
            JOIN material_inquiries mi ON i.inquiry_id = mi.id
            JOIN projects p ON mi.project_id = p.id
            WHERE i.id = %s AND i.inquiry_id = %s
            """,
            (payload.awarded_invitation_id, inq_id),
        )
        if not awarded_data:
            raise HTTPException(status_code=400, detail="邀请不属于该询价或没有报价")

    now = datetime.utcnow()
    po_id = None
    po_no = None

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1) 记审批记录
            cur.execute(
                """
                INSERT INTO workflow_approval_records
                    (id, instance_id, node_id, action, actor_id,
                     assignee_type, assignee_id, comment, metadata_json, created_at)
                VALUES (%s, %s::uuid, %s, %s, %s, 'role', 'ADMIN', %s, %s, %s)
                """,
                (str(uuid.uuid4()), task["instance_hex"], node, payload.action,
                 str(user.user_id), payload.comment or "",
                 json.dumps({"awarded_invitation_id":payload.awarded_invitation_id},
                            ensure_ascii=False), now),
            )
            cur.execute(
                "UPDATE workflow_approval_tasks SET status='completed', claimed_by=%s, "
                "completed_at=%s, completion_action=%s WHERE id=%s",
                (str(user.user_id), now, payload.action, task_id),
            )

            if payload.action == "approve":
                # 生成 material_purchase_orders
                total = float(awarded_data["unit_price"]) * float(awarded_data["qty"])
                po_no = f"MP-{awarded_data['project_no']}-{uuid.uuid4().hex[:4].upper()}"

                cur.execute(
                    """
                    INSERT INTO material_purchase_orders
                        (po_no, project_id, supplier_id, tenant_id,
                         material_code, spec, qty, unit, unit_price, total_amount,
                         required_date, status, created_by,
                         sourced_from_inquiry_id, outsource_order_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'sent', %s, %s, %s)
                    RETURNING id
                    """,
                    (po_no, awarded_data["project_id"], awarded_data["supplier_id"],
                     awarded_data["material_tenant_id"],
                     awarded_data["material_code"], awarded_data["spec"],
                     awarded_data["qty"], awarded_data["unit"],
                     float(awarded_data["unit_price"]), total,
                     awarded_data["required_date"], user.user_id,
                     inq_id, awarded_data["outsource_order_id"]),
                )
                po_id = cur.fetchone()[0]

                cur.execute(
                    "UPDATE material_inquiries SET status='awarded', winning_quotation_id=%s WHERE id=%s",
                    (awarded_data["quotation_id"], inq_id),
                )
            else:
                cur.execute("UPDATE material_inquiries SET status='cancelled' WHERE id=%s", (inq_id,))

    return {
        "status": "ok",
        "action": payload.action,
        "material_po_id": po_id,
        "material_po_no": po_no,
    }


# =============================================================================
# 4. 材料方视角：看询价邀请 + 报价
# =============================================================================

def _my_supplier_id(user: CurrentUser) -> Optional[int]:
    row = db.fetch_one("SELECT supplier_id FROM tenants WHERE id=%s", (user.tenant_id,))
    return (row or {}).get("supplier_id")


@material_router.get("/inquiries")
async def list_my_inquiries(user: CurrentUser = Depends(_require_material)):
    rows = db.fetch_all(
        """
        SELECT i.id AS invitation_id, i.invitation_status, i.sent_at, i.quoted_at,
               mi.id AS inquiry_id, mi.inquiry_no, mi.title,
               mi.material_code, mi.material_name, mi.spec, mi.qty, mi.unit,
               mi.required_date, mi.status AS inquiry_status,
               p.project_no, p.name AS project_name
        FROM material_inquiry_invitations i
        JOIN material_inquiries mi ON i.inquiry_id = mi.id
        JOIN projects p ON mi.project_id = p.id
        WHERE i.tenant_id = %s
        ORDER BY i.sent_at DESC
        """,
        (user.tenant_id,),
    )
    for r in rows:
        if r.get("qty") is not None: r["qty"] = float(r["qty"])
    stats = {
        "pending": sum(1 for r in rows if r["invitation_status"] == "sent"),
        "quoted":  sum(1 for r in rows if r["invitation_status"] == "quoted"),
        "expired": sum(1 for r in rows if r["invitation_status"] == "no_response"),
    }
    return {"items": rows, "stats": stats, "total": len(rows)}


@material_router.get("/inquiries/{inv_id}")
async def get_my_inquiry(
    inv_id: int,
    user: CurrentUser = Depends(_require_material),
):
    row = db.fetch_one(
        """
        SELECT i.*, mi.id AS inquiry_id, mi.inquiry_no, mi.title,
               mi.material_code, mi.material_name, mi.spec, mi.qty, mi.unit,
               mi.required_date, mi.status AS inquiry_status,
               p.project_no, p.name AS project_name
        FROM material_inquiry_invitations i
        JOIN material_inquiries mi ON i.inquiry_id = mi.id
        JOIN projects p ON mi.project_id = p.id
        WHERE i.id = %s
        """,
        (inv_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if row.get("qty") is not None: row["qty"] = float(row["qty"])

    my_quote = db.fetch_one(
        "SELECT id, unit_price, lead_time_days, note, submitted_at "
        "FROM material_quotations WHERE invitation_id=%s",
        (inv_id,),
    )
    if my_quote and my_quote.get("unit_price"):
        my_quote["unit_price"] = float(my_quote["unit_price"])
        if my_quote.get("submitted_at"):
            my_quote["submitted_at"] = my_quote["submitted_at"].isoformat()

    return {"invitation": row, "my_quote": my_quote}


@material_router.post("/inquiries/{inv_id}/quote")
async def submit_material_quote(
    inv_id: int,
    payload: InquiryQuoteSubmit,
    user: CurrentUser = Depends(_require_material),
):
    row = db.fetch_one(
        """
        SELECT i.*, mi.status AS inquiry_status
        FROM material_inquiry_invitations i
        JOIN material_inquiries mi ON i.inquiry_id = mi.id
        WHERE i.id = %s
        """,
        (inv_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if row["inquiry_status"] != "inviting":
        raise HTTPException(status_code=409,
                            detail=f"询价单状态 '{row['inquiry_status']}' 不再接受报价")

    now = datetime.utcnow()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO material_quotations
                    (invitation_id, unit_price, lead_time_days, note, submitted_by, submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (invitation_id) DO UPDATE SET
                    unit_price = EXCLUDED.unit_price,
                    lead_time_days = EXCLUDED.lead_time_days,
                    note = EXCLUDED.note,
                    submitted_by = EXCLUDED.submitted_by,
                    submitted_at = EXCLUDED.submitted_at
                """,
                (inv_id, payload.unit_price, payload.lead_time_days,
                 payload.note, user.user_id, now),
            )
            cur.execute(
                "UPDATE material_inquiry_invitations SET invitation_status='quoted', quoted_at=%s WHERE id=%s",
                (now, inv_id),
            )
    return {"status": "quoted", "submitted_at": now.isoformat()}


# =============================================================================
# 5. 加工方自采凭证（processor_source 模式）
# =============================================================================

_PROOF_UPLOAD = Path(__file__).resolve().parent.parent.parent / "uploads" / "proofs"
_PROOF_UPLOAD.mkdir(parents=True, exist_ok=True)


def _my_tenant_id_proc(user: CurrentUser) -> int:
    return user.tenant_id


@processor_router.get("/orders/{order_id}/proofs")
async def list_proofs(
    order_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    # 归属校验
    o = db.fetch_one("SELECT tenant_id FROM outsource_orders WHERE id=%s", (order_id,))
    if o is None: raise HTTPException(status_code=404)
    if o["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)

    rows = db.fetch_all(
        """
        SELECT pmp.*, a.file_name, a.file_path, a.file_size, a.mime_type,
               u.display_name AS uploader_name
        FROM processor_material_proofs pmp
        LEFT JOIN attachments a ON pmp.attachment_id = a.id
        LEFT JOIN users u ON pmp.uploaded_by = u.id
        WHERE pmp.outsource_order_id = %s
        ORDER BY pmp.created_at DESC
        """,
        (order_id,),
    )
    for r in rows:
        if r.get("qty"): r["qty"] = float(r["qty"])
    return {"items": rows, "total": len(rows)}


@processor_router.post("/orders/{order_id}/proofs", status_code=201)
async def upload_proof(
    order_id: int,
    proof_type: str = Form(...),          # invoice/contract/photo/certificate/waybill
    supplier_name_text: str = Form(""),
    batch_no: str = Form(""),
    material_code: str = Form(""),
    spec: str = Form(""),
    qty: Optional[float] = Form(None),
    unit: str = Form("kg"),
    purchase_date: Optional[str] = Form(None),
    note: str = Form(""),
    file: Optional[UploadFile] = File(None),
    user: CurrentUser = Depends(_require_processor),
):
    o = db.fetch_one(
        "SELECT tenant_id, material_sourcing, status FROM outsource_orders WHERE id=%s",
        (order_id,),
    )
    if o is None: raise HTTPException(status_code=404)
    if o["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if o["material_sourcing"] != "processor":
        raise HTTPException(status_code=409,
                            detail="仅 material_sourcing='processor' 的订单可上传自采凭证")

    attachment_id = None
    if file is not None:
        ext = Path(file.filename or "").suffix
        stored = f"o{order_id}_{uuid.uuid4().hex[:8]}{ext}"
        target = _PROOF_UPLOAD / stored
        import shutil
        with target.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        size = target.stat().st_size
        attachment_id = db.execute(
            """
            INSERT INTO attachments
                (related_type, related_id, file_name, file_path, file_size,
                 mime_type, uploaded_by, category)
            VALUES ('outsource_order', %s, %s, %s, %s, %s, %s, 'proof')
            RETURNING id
            """,
            (order_id, file.filename, f"proofs/{stored}", size,
             file.content_type, user.user_id),
        )

    new_id = db.execute(
        """
        INSERT INTO processor_material_proofs
            (outsource_order_id, proof_type, attachment_id,
             supplier_name_text, batch_no, material_code, spec, qty, unit,
             purchase_date, note, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (order_id, proof_type, attachment_id,
         supplier_name_text or None, batch_no or None,
         material_code or None, spec or None, qty, unit,
         purchase_date, note or None, user.user_id),
    )
    return {"id": new_id, "attachment_id": attachment_id}


@processor_router.delete("/orders/{order_id}/proofs/{proof_id}")
async def delete_proof(
    order_id: int,
    proof_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    row = db.fetch_one(
        """
        SELECT pmp.id, o.tenant_id
        FROM processor_material_proofs pmp
        JOIN outsource_orders o ON pmp.outsource_order_id = o.id
        WHERE pmp.id = %s AND pmp.outsource_order_id = %s
        """,
        (proof_id, order_id),
    )
    if row is None: raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    db.execute("DELETE FROM processor_material_proofs WHERE id=%s", (proof_id,))
    return {"deleted": True}
