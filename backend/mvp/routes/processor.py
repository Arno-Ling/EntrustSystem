"""
Processor tenant routes (加工方视角).

Stage 3:
  - 查看分配给我的询价邀请
  - 查看邀请详情（零件清单 + 图纸）
  - 提交报价
  - 查看我的加工单
  - 推进加工单状态（accepted → producing → delivered）
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

router = APIRouter(prefix="/api/processor", tags=["processor"])

_require_processor = require_tenant_type("processor")


class QuoteLine(BaseModel):
    scope_item_id:   int
    unit_price:      float = Field(..., gt=0)
    lead_time_days:  int = Field(..., ge=1)
    material_unit_price: Optional[float] = None
    note: Optional[str] = None


class QuoteSubmit(BaseModel):
    """报价提交。两种模式：
      - 传统（兼容）：只填 unit_price/lead_time_days/note（整单一口价）
      - 逐项：lines = [QuoteLine, ...]；unit_price 视作汇总价，后端自动聚合
    """
    unit_price: Optional[float] = None
    lead_time_days: Optional[int] = None
    note: Optional[str] = None
    lines: Optional[list[QuoteLine]] = None


class StatusTransition(BaseModel):
    to_status: str = Field(..., pattern="^(accepted|producing|delivered|cancelled)$")
    note: Optional[str] = None


# Valid order status transitions
ORDER_TRANSITIONS = {
    "awarded": {"accepted", "cancelled"},
    "accepted": {"producing", "cancelled"},
    "producing": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}


def _my_tenant_id(user: CurrentUser) -> int:
    """Convenience — processor user's tenant id。"""
    return user.tenant_id


# =============================================================================
# 询价邀请
# =============================================================================

@router.get("/invitations")
async def list_invitations(
    user: CurrentUser = Depends(_require_processor),
    status_filter: Optional[str] = None,
):
    """只看 tenant_id = 我的租户 的邀请。"""
    where = "WHERE i.tenant_id = %s"
    params: list[Any] = [_my_tenant_id(user)]
    if status_filter:
        where += " AND i.invitation_status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT i.id, i.request_id, i.invitation_status,
               i.sent_at, i.quoted_at,
               r.title AS request_title, r.required_processes_json,
               r.quantity, r.deadline, r.status AS request_status,
               p.project_no, p.name AS project_name,
               q.id AS quotation_id, q.unit_price, q.lead_time_days, q.submitted_at
        FROM outsource_request_invitations i
        JOIN outsource_requests r ON i.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        LEFT JOIN outsource_quotations q ON q.invitation_id = i.id
        {where}
        ORDER BY i.sent_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        raw = r.get("required_processes_json")
        r["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
        r.pop("required_processes_json", None)
        if r.get("unit_price") is not None:
            r["unit_price"] = float(r["unit_price"])
        for k in ("sent_at", "quoted_at", "submitted_at"):
            if r.get(k):
                r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else r[k]

    # 统计
    stats = {
        "pending_quote": sum(1 for r in rows if r["invitation_status"] == "sent"),
        "quoted": sum(1 for r in rows if r["invitation_status"] == "quoted"),
        "expired": sum(1 for r in rows if r["invitation_status"] == "no_response"),
    }
    return {"items": rows, "total": len(rows), "stats": stats}


@router.get("/invitations/{inv_id}")
async def get_invitation(
    inv_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    inv = db.fetch_one(
        """
        SELECT i.*, r.id AS req_id, r.title AS request_title,
               r.required_processes_json, r.quantity, r.deadline,
               r.status AS request_status, r.material_sourcing,
               p.id AS project_id, p.project_no, p.name AS project_name,
               p.customer, p.unit_price AS client_unit_price
        FROM outsource_request_invitations i
        JOIN outsource_requests r ON i.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE i.id = %s
        """,
        (inv_id,),
    )
    if inv is None:
        raise HTTPException(status_code=404)
    if inv["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403, detail="Not your invitation")

    raw = inv.get("required_processes_json")
    inv["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    inv.pop("required_processes_json", None)

    # 零件清单（用于加工方估报价）
    parts = db.fetch_all(
        """
        SELECT id, part_no, part_name, material, qty, processes_json, spec
        FROM project_parts WHERE project_id = %s
        """,
        (inv["project_id"],),
    )
    for p in parts:
        raw = p.get("processes_json")
        p["processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
        p.pop("processes_json", None)

    # 图纸列表（下载链接由 internal 端控制；MVP 简化：加工方可直接下）
    attachments = db.fetch_all(
        """
        SELECT id, file_name, file_size, mime_type, created_at
        FROM attachments
        WHERE related_type = 'project' AND related_id = %s
        ORDER BY created_at DESC
        """,
        (inv["project_id"],),
    )

    # 我的报价（若已提交）
    my_quote = db.fetch_one(
        "SELECT id, unit_price, lead_time_days, note, submitted_at FROM outsource_quotations WHERE invitation_id = %s",
        (inv_id,),
    )
    if my_quote and my_quote.get("unit_price") is not None:
        my_quote["unit_price"] = float(my_quote["unit_price"])
        if my_quote.get("submitted_at"):
            my_quote["submitted_at"] = my_quote["submitted_at"].isoformat()

    # 委外项清单（5 层粒度）
    scope_items = db.fetch_all(
        """
        SELECT * FROM v_outsource_scope_display
        WHERE request_id = %s
        ORDER BY scope_item_id
        """,
        (inv["req_id"],),
    )

    # 我的报价明细（若已填过 lines）
    my_quote_lines = []
    if my_quote:
        my_quote_lines = db.fetch_all(
            """
            SELECT ql.id, ql.scope_item_id, ql.unit_price, ql.lead_time_days, ql.note
            FROM outsource_quotation_lines ql
            WHERE ql.quotation_id = %s
            """,
            (my_quote["id"],),
        )
        for l in my_quote_lines:
            l["unit_price"] = float(l["unit_price"])

    return {
        "invitation": inv,
        "parts": parts,
        "attachments": attachments,
        "my_quote": my_quote,
        "scope_items": scope_items,
        "my_quote_lines": my_quote_lines,
    }


@router.post("/invitations/{inv_id}/quote")
async def submit_quote(
    inv_id: int,
    payload: QuoteSubmit,
    user: CurrentUser = Depends(_require_processor),
):
    inv = db.fetch_one(
        """
        SELECT i.*, r.status AS request_status, r.material_sourcing
        FROM outsource_request_invitations i
        JOIN outsource_requests r ON i.request_id = r.id
        WHERE i.id = %s
        """,
        (inv_id,),
    )
    if inv is None:
        raise HTTPException(status_code=404)
    if inv["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403)
    if inv["request_status"] not in ("inviting",):
        raise HTTPException(status_code=409,
                            detail=f"询价单当前状态 '{inv['request_status']}' 不再接受报价")
    if inv["invitation_status"] not in ("sent", "quoted"):
        raise HTTPException(status_code=409,
                            detail=f"邀请状态 '{inv['invitation_status']}' 不允许报价")

    # 支持两种模式：传统整单一口价 / 逐项 lines
    lines = payload.lines or []
    material_sourcing = inv.get("material_sourcing")

    if lines:
        # 加工方自采模式：校验 material_unit_price
        if material_sourcing == "processor":
            for l in lines:
                if l.material_unit_price is None or l.material_unit_price <= 0:
                    raise HTTPException(status_code=400,
                                        detail="加工方自采模式下必须填写每行材料单价")
        # 汇总：unit_price = sum(line_price), lead_time_days = max(line)
        agg_price = sum(l.unit_price for l in lines)
        agg_lead = max(l.lead_time_days for l in lines)
    else:
        if payload.unit_price is None or payload.lead_time_days is None:
            raise HTTPException(status_code=400,
                                detail="传统报价模式需要 unit_price + lead_time_days")
        agg_price = payload.unit_price
        agg_lead  = payload.lead_time_days

    now = datetime.utcnow()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # Upsert 汇总报价 (1 invitation → 1 quote)
            cur.execute(
                """
                INSERT INTO outsource_quotations
                    (invitation_id, unit_price, lead_time_days, note, submitted_by, submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (invitation_id) DO UPDATE SET
                    unit_price = EXCLUDED.unit_price,
                    lead_time_days = EXCLUDED.lead_time_days,
                    note = EXCLUDED.note,
                    submitted_by = EXCLUDED.submitted_by,
                    submitted_at = EXCLUDED.submitted_at
                RETURNING id
                """,
                (inv_id, agg_price, agg_lead,
                 payload.note, user.user_id, now),
            )
            quotation_id = cur.fetchone()[0]

            # 逐项 lines：清空旧行，写新行
            if lines:
                cur.execute(
                    "DELETE FROM outsource_quotation_lines WHERE quotation_id = %s",
                    (quotation_id,),
                )
                for l in lines:
                    # internal 模式下忽略 material_unit_price
                    mat_price = l.material_unit_price if material_sourcing == "processor" else None
                    cur.execute(
                        """
                        INSERT INTO outsource_quotation_lines
                            (quotation_id, scope_item_id, unit_price, lead_time_days, note, material_unit_price)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (quotation_id, l.scope_item_id,
                         l.unit_price, l.lead_time_days, l.note, mat_price),
                    )

            cur.execute(
                """
                UPDATE outsource_request_invitations
                SET invitation_status = 'quoted', quoted_at = %s
                WHERE id = %s
                """,
                (now, inv_id),
            )

    return {
        "status": "quoted",
        "quotation_id": quotation_id,
        "submitted_at": now.isoformat(),
        "total_price": agg_price,
        "lead_time_days": agg_lead,
        "line_count": len(lines),
    }


# =============================================================================
# 加工单
# =============================================================================

@router.get("/orders")
async def list_orders(
    user: CurrentUser = Depends(_require_processor),
    status_filter: Optional[str] = None,
):
    where = "WHERE o.tenant_id = %s"
    params: list[Any] = [_my_tenant_id(user)]
    if status_filter:
        where += " AND o.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT o.id, o.order_no, o.unit_price, o.quantity, o.total_amount,
               o.lead_time_days, o.status,
               o.awarded_at, o.accepted_at, o.delivered_at,
               r.title AS request_title,
               p.project_no, p.name AS project_name
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        {where}
        ORDER BY o.awarded_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        if r.get("unit_price"): r["unit_price"] = float(r["unit_price"])
        if r.get("total_amount"): r["total_amount"] = float(r["total_amount"])
        for k in ("awarded_at", "accepted_at", "delivered_at"):
            if r.get(k):
                r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else r[k]
    return {"items": rows, "total": len(rows)}


@router.get("/orders/{order_id}")
async def get_order(
    order_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    row = db.fetch_one(
        """
        SELECT o.*, r.title AS request_title, r.required_processes_json,
               p.project_no, p.name AS project_name, p.deadline AS project_deadline
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE o.id = %s
        """,
        (order_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403)

    raw = row.get("required_processes_json")
    row["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    row.pop("required_processes_json", None)

    # 转数值 + 时间
    for k in ("unit_price", "total_amount"):
        if row.get(k) is not None:
            row[k] = float(row[k])
    for k in ("awarded_at", "accepted_at", "delivered_at", "created_at", "updated_at"):
        if row.get(k):
            row[k] = row[k].isoformat() if hasattr(row[k], "isoformat") else row[k]

    # 零件清单
    parts = db.fetch_all(
        """
        SELECT part_no, part_name, material, qty, processes_json, spec
        FROM project_parts pp
        WHERE pp.project_id = (SELECT project_id FROM outsource_requests WHERE id = %s)
        """,
        (row["request_id"],),
    )
    for p in parts:
        raw = p.get("processes_json")
        p["processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
        p.pop("processes_json", None)

    # 状态事件
    events = db.fetch_all(
        """
        SELECT from_status, to_status, note, occurred_at
        FROM outsource_order_status_events
        WHERE order_id = %s ORDER BY occurred_at ASC
        """,
        (order_id,),
    )
    for e in events:
        if e.get("occurred_at"):
            e["occurred_at"] = e["occurred_at"].isoformat()

    # 同一项目的材料方发货照（供加工方对比）
    shipments_photos = []
    ship_rows = db.fetch_all(
        """
        SELECT sh.shipment_no, sh.batch_no, sh.qty_shipped, sh.carrier,
               sh.photo_paths, sh.shipped_at,
               mpo.po_no, mpo.material_code, mpo.spec
        FROM material_shipments sh
        JOIN material_purchase_orders mpo ON sh.po_id = mpo.id
        WHERE mpo.project_id = (SELECT project_id FROM outsource_requests WHERE id = %s)
          AND sh.photo_paths IS NOT NULL
        ORDER BY sh.shipped_at DESC
        """,
        (row["request_id"],),
    )
    for r in ship_rows:
        if r.get("qty_shipped"): r["qty_shipped"] = float(r["qty_shipped"])
        if r.get("shipped_at"):
            r["shipped_at"] = r["shipped_at"].isoformat() if hasattr(r["shipped_at"], "isoformat") else r["shipped_at"]
        # 展平成一个数组
        shipments_photos.extend(r.get("photo_paths") or [])

    return {
        "order": row,
        "parts": parts,
        "events": events,
        "shipments_photos": shipments_photos,
        "shipments_detail": ship_rows,
    }


@router.post("/orders/{order_id}/status")
async def update_order_status(
    order_id: int,
    payload: StatusTransition,
    user: CurrentUser = Depends(_require_processor),
):
    row = db.fetch_one(
        """
        SELECT id, status, tenant_id,
               receive_photos, complete_photos
        FROM outsource_orders WHERE id = %s
        """,
        (order_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403)

    current = row["status"]
    allowed = ORDER_TRANSITIONS.get(current, set())
    if payload.to_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"状态转换不合法: {current} → {payload.to_status}；允许: {sorted(allowed)}",
        )

    # 拍照取证硬校验
    if payload.to_status == "accepted":
        if not (row["receive_photos"] and len(row["receive_photos"]) > 0):
            raise HTTPException(status_code=400,
                                detail="接单前必须上传至少 1 张【收料照片】")
    if payload.to_status == "delivered":
        if not (row["complete_photos"] and len(row["complete_photos"]) > 0):
            raise HTTPException(status_code=400,
                                detail="交货前必须上传至少 1 张【成品照片】")

    now = datetime.utcnow()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # Timestamps
            ts_update = ""
            if payload.to_status == "accepted":
                ts_update = ", accepted_at = NOW()"
            elif payload.to_status == "delivered":
                ts_update = ", delivered_at = NOW()"

            cur.execute(
                f"UPDATE outsource_orders SET status = %s{ts_update} WHERE id = %s",
                (payload.to_status, order_id),
            )
            cur.execute(
                """
                INSERT INTO outsource_order_status_events
                    (order_id, from_status, to_status, changed_by, note, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (order_id, current, payload.to_status, user.user_id, payload.note, now),
            )

    return {"status": payload.to_status, "occurred_at": now.isoformat()}


# =============================================================================
# 拍照取证：加工方上传照片
# =============================================================================

import shutil as _shutil
import uuid as _uuid


@router.post("/orders/{order_id}/photos")
async def upload_order_photo(
    order_id: int,
    stage: str = Form(...),                # receive / complete
    note: str = Form(""),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_require_processor),
):
    """上传加工单照片。stage=receive（接单时收料）/ complete（完工交货）。

    同一 stage 可多次调用，每次追加一张。
    """
    if stage not in ("receive", "complete"):
        raise HTTPException(status_code=400, detail="stage 必须是 receive 或 complete")

    row = db.fetch_one(
        """
        SELECT id, status, tenant_id,
               receive_photos, complete_photos
        FROM outsource_orders WHERE id = %s
        """,
        (order_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403)

    # 状态约束：receive 只能在 awarded 阶段上传；complete 必须在 producing
    if stage == "receive" and row["status"] != "awarded":
        raise HTTPException(status_code=409,
                            detail=f"收料照只能在 'awarded' 状态上传，当前 {row['status']}")
    if stage == "complete" and row["status"] != "producing":
        raise HTTPException(status_code=409,
                            detail=f"成品照只能在 'producing' 状态上传，当前 {row['status']}")

    # 保存文件
    target_dir = UPLOADS_DIR / "orders"
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix
    stored = f"o{order_id}_{stage}_{_uuid.uuid4().hex[:8]}{ext}"
    target = target_dir / stored
    with target.open("wb") as f:
        _shutil.copyfileobj(file.file, f)
    size = target.stat().st_size
    rel = f"orders/{stored}"

    # 追加到数组
    col = f"{stage}_photos"
    notes_col = f"{stage}_notes"
    existing = row[col] or []
    existing = [*existing, rel]

    # 更新
    db.execute(
        f"UPDATE outsource_orders SET {col} = %s, {notes_col} = COALESCE(NULLIF(%s, ''), {notes_col}) WHERE id = %s",
        (existing, note, order_id),
    )

    return {
        "path": rel,
        "url": f"/uploads/{rel}",
        "file_name": file.filename,
        "file_size": size,
        "total_photos": len(existing),
    }


@router.delete("/orders/{order_id}/photos")
async def delete_order_photo(
    order_id: int,
    stage: str,
    path: str,
    user: CurrentUser = Depends(_require_processor),
):
    """删除一张照片（前端误传时用）。"""
    if stage not in ("receive", "complete"):
        raise HTTPException(status_code=400)

    row = db.fetch_one(
        "SELECT tenant_id, receive_photos, complete_photos FROM outsource_orders WHERE id=%s",
        (order_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != _my_tenant_id(user):
        raise HTTPException(status_code=403)

    col = f"{stage}_photos"
    existing = row[col] or []
    new_list = [p for p in existing if p != path]

    db.execute(f"UPDATE outsource_orders SET {col} = %s WHERE id = %s",
               (new_list, order_id))
    return {"remaining": len(new_list)}


# =============================================================================
# Attachment download (proxy via internal)
# =============================================================================

from fastapi.responses import FileResponse
from pathlib import Path


UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    """加工方可下载与其邀请/订单关联的项目附件。"""
    att = db.fetch_one(
        """
        SELECT a.file_name, a.file_path, a.mime_type, a.related_id AS project_id
        FROM attachments a
        WHERE a.id = %s AND a.related_type = 'project'
        """,
        (attachment_id,),
    )
    if att is None:
        raise HTTPException(status_code=404)

    # 授权：这个 project 必须有发给我的邀请或者加工单
    has_access = db.fetch_one(
        """
        SELECT 1 AS ok FROM outsource_request_invitations i
        JOIN outsource_requests r ON i.request_id = r.id
        WHERE i.tenant_id = %s AND r.project_id = %s
        LIMIT 1
        """,
        (_my_tenant_id(user), att["project_id"]),
    )
    if not has_access:
        raise HTTPException(status_code=403, detail="您没有访问该项目附件的权限")

    full = UPLOADS_DIR / att["file_path"]
    if not full.exists():
        raise HTTPException(status_code=410, detail="File missing")

    return FileResponse(
        path=str(full),
        filename=att["file_name"],
        media_type=att.get("mime_type") or "application/octet-stream",
    )


# =============================================================================
# 返工单（加工方视角）
# =============================================================================

class ReworkConfirm(BaseModel):
    remark: Optional[str] = None


class ReworkDeliver(BaseModel):
    remark: Optional[str] = None


REWORK_TRANSITIONS = {
    "pending":     {"confirmed", "cancelled"},
    "confirmed":   {"in_progress", "cancelled"},
    "in_progress": {"delivered", "cancelled"},
    "delivered":   {"inspecting", "cancelled"},
    "inspecting":  {"completed", "cancelled"},
    "completed":   set(),
    "cancelled":   set(),
}


def _rework_tenant_check(rework_id: int, user: CurrentUser) -> dict | None:
    """校验返工单归属加工方。返回 rework_order 行或 None。"""
    row = db.fetch_one(
        """
        SELECT rw.*, o.tenant_id AS order_tenant_id
        FROM rework_orders rw
        LEFT JOIN outsource_orders o ON rw.original_order_id = o.id
        WHERE rw.id = %s
        """,
        (rework_id,),
    )
    if row is None:
        return None
    if row["order_tenant_id"] != _my_tenant_id(user):
        return None
    return row


@router.get("/rework-orders")
async def list_rework_orders(user: CurrentUser = Depends(_require_processor)):
    """加工方查看自己的返工单列表。"""
    rows = db.fetch_all(
        """
        SELECT rw.id, rw.rework_no, rw.status, rw.rework_type,
               rw.material_sourcing_mode, rw.cost_bearer,
               rw.confirmed_at, rw.delivered_at, rw.completed_at,
               rw.created_at,
               p.project_no, p.name AS project_name,
               e.exception_no, e.exception_type, e.severity
        FROM rework_orders rw
        JOIN projects p ON rw.project_id = p.id
        LEFT JOIN quality_exceptions e ON rw.exception_id = e.id
        LEFT JOIN outsource_orders o ON rw.original_order_id = o.id
        WHERE o.tenant_id = %s
        ORDER BY rw.created_at DESC
        LIMIT 100
        """,
        (_my_tenant_id(user),),
    )
    stats = {
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "in_progress": sum(1 for r in rows if r["status"] == "in_progress"),
        "delivered": sum(1 for r in rows if r["status"] == "delivered"),
        "completed": sum(1 for r in rows if r["status"] == "completed"),
    }
    return {"items": rows, "total": len(rows), "stats": stats}


@router.get("/rework-orders/{rework_id}")
async def get_rework_order(
    rework_id: int,
    user: CurrentUser = Depends(_require_processor),
):
    """加工方查看返工单详情。"""
    row = _rework_tenant_check(rework_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="Rework order not found")

    # 关联的异常信息
    exc = db.fetch_one(
        "SELECT exception_no, exception_type, severity, description FROM quality_exceptions WHERE id=%s",
        (row["exception_id"],),
    )

    return {
        "rework_order": row,
        "exception": exc,
    }


@router.post("/rework-orders/{rework_id}/confirm")
async def confirm_rework(
    rework_id: int,
    payload: ReworkConfirm,
    user: CurrentUser = Depends(_require_processor),
):
    """加工方确认返工单。"""
    row = _rework_tenant_check(rework_id, user)
    if row is None:
        raise HTTPException(status_code=404)
    if row["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"返工单当前状态 '{row['status']}' 不允许确认，需为 pending")

    from mvp.rework_service import process_rework_confirmation

    now = datetime.utcnow()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 确认
            cur.execute(
                """
                UPDATE rework_orders
                SET status='confirmed', confirmed_at=%s, confirmed_by=%s, confirm_remark=%s
                WHERE id=%s
                """,
                (now, user.user_id, payload.remark, rework_id),
            )
            # 分支处理
            result = process_rework_confirmation(rework_id, cur)

    return {
        "rework_id": rework_id,
        "status": "in_progress",
        "branch": result["branch"],
        "material_sourcing_mode": result["material_sourcing_mode"],
        "new_po_id": result["new_po_id"],
    }


@router.post("/rework-orders/{rework_id}/deliver")
async def deliver_rework(
    rework_id: int,
    payload: ReworkDeliver,
    user: CurrentUser = Depends(_require_processor),
):
    """加工方标记返工交货。"""
    row = _rework_tenant_check(rework_id, user)
    if row is None:
        raise HTTPException(status_code=404)
    if row["status"] != "in_progress":
        raise HTTPException(status_code=409,
                            detail=f"返工单当前状态 '{row['status']}' 不允许交货，需为 in_progress")

    # 校验照片
    photos = row.get("deliver_photos") or []
    if not photos:
        raise HTTPException(status_code=400,
                            detail="交货前必须上传至少 1 张照片")

    now = datetime.utcnow()
    db.execute(
        """
        UPDATE rework_orders
        SET status='delivered', delivered_at=%s, deliver_remark=%s
        WHERE id=%s
        """,
        (now, payload.remark, rework_id),
    )
    return {"rework_id": rework_id, "status": "delivered", "delivered_at": now.isoformat()}


@router.post("/rework-orders/{rework_id}/photos")
async def upload_rework_photo(
    rework_id: int,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_require_processor),
):
    """上传返工照片。"""
    row = _rework_tenant_check(rework_id, user)
    if row is None:
        raise HTTPException(status_code=404)

    # 保存文件
    target_dir = UPLOADS_DIR / "rework"
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix
    stored = f"rw{rework_id}_{_uuid.uuid4().hex[:8]}{ext}"
    target = target_dir / stored
    with target.open("wb") as f:
        _shutil.copyfileobj(file.file, f)
    rel = f"rework/{stored}"

    # 追加到 deliver_photos
    existing = row.get("deliver_photos") or []
    existing = [*existing, rel]
    db.execute(
        "UPDATE rework_orders SET deliver_photos = %s WHERE id = %s",
        (json.dumps(existing), rework_id),
    )
    return {"path": rel, "url": f"/uploads/{rel}", "total_photos": len(existing)}


@router.delete("/rework-orders/{rework_id}/photos")
async def delete_rework_photo(
    rework_id: int,
    path: str,
    user: CurrentUser = Depends(_require_processor),
):
    """删除一张返工照片。"""
    row = _rework_tenant_check(rework_id, user)
    if row is None:
        raise HTTPException(status_code=404)

    existing = row.get("deliver_photos") or []
    new_list = [p for p in existing if p != path]
    db.execute(
        "UPDATE rework_orders SET deliver_photos = %s WHERE id = %s",
        (json.dumps(new_list), rework_id),
    )
    return {"remaining": len(new_list)}
