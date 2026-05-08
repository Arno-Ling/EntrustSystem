"""
Stage 4-SELF — 材料方视角 (tenant_type = 'material')

材料方只能看到与自己有关的采购订单 (supplier_id 关联)：
  - /api/material/pos                 我的采购订单列表
  - /api/material/pos/{id}            订单详情
  - /api/material/pos/{id}/accept     接单
  - /api/material/pos/{id}/ship       发货（含备料照片 + 批次号）
  - /api/material/shipments           我的发货历史
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

router = APIRouter(prefix="/api/material", tags=["material"])
_require_material = require_tenant_type("material")


def _my_supplier_id(user: CurrentUser) -> Optional[int]:
    row = db.fetch_one("SELECT supplier_id FROM tenants WHERE id = %s", (user.tenant_id,))
    return (row or {}).get("supplier_id")


# =============================================================================
# Pydantic
# =============================================================================

class ShipmentCreate(BaseModel):
    qty_shipped: float = Field(..., gt=0)
    batch_no: Optional[str] = None
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None
    photo_paths: Optional[list[str]] = None
    remark: Optional[str] = None


# =============================================================================
# 采购单（PO）
# =============================================================================

@router.get("/pos")
async def list_pos(
    status_filter: Optional[str] = None,
    user: CurrentUser = Depends(_require_material),
):
    sid = _my_supplier_id(user)
    if sid is None:
        return {"items": [], "total": 0}

    where = "WHERE mpo.supplier_id = %s"
    params: list[Any] = [sid]
    if status_filter:
        where += " AND mpo.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT mpo.*, p.project_no, p.name AS project_name,
               m.mold_no
        FROM material_purchase_orders mpo
        JOIN projects p ON mpo.project_id = p.id
        LEFT JOIN molds m ON mpo.mold_id = m.id
        {where}
        ORDER BY mpo.created_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        if r.get("unit_price") is not None:
            r["unit_price"] = float(r["unit_price"])
        if r.get("total_amount") is not None:
            r["total_amount"] = float(r["total_amount"])
        if r.get("qty") is not None:
            r["qty"] = float(r["qty"])
        if r.get("moq_surplus_qty") is not None:
            r["moq_surplus_qty"] = float(r["moq_surplus_qty"])

    # 统计
    stats = {
        "pending_accept": sum(1 for r in rows if r["status"] in ("drafted","sent")),
        "accepted":       sum(1 for r in rows if r["status"] == "accepted"),
        "shipped":        sum(1 for r in rows if r["status"] == "shipped"),
        "received":       sum(1 for r in rows if r["status"] == "received"),
    }
    return {"items": rows, "total": len(rows), "stats": stats}


@router.get("/pos/{po_id}")
async def get_po(po_id: int, user: CurrentUser = Depends(_require_material)):
    sid = _my_supplier_id(user)
    row = db.fetch_one(
        """
        SELECT mpo.*, p.project_no, p.name AS project_name,
               p.customer, m.mold_no, m.name AS mold_name
        FROM material_purchase_orders mpo
        JOIN projects p ON mpo.project_id = p.id
        LEFT JOIN molds m ON mpo.mold_id = m.id
        WHERE mpo.id = %s
        """,
        (po_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["supplier_id"] != sid:
        raise HTTPException(status_code=403)

    for k in ("unit_price", "total_amount", "qty", "moq_surplus_qty"):
        if row.get(k) is not None:
            row[k] = float(row[k])

    # 发货记录
    shipments = db.fetch_all(
        "SELECT * FROM material_shipments WHERE po_id = %s ORDER BY created_at DESC",
        (po_id,),
    )
    for s in shipments:
        if s.get("qty_shipped") is not None:
            s["qty_shipped"] = float(s["qty_shipped"])

    return {"po": row, "shipments": shipments}


@router.post("/pos/{po_id}/accept")
async def accept_po(po_id: int, user: CurrentUser = Depends(_require_material)):
    sid = _my_supplier_id(user)
    row = db.fetch_one(
        "SELECT id, status, supplier_id FROM material_purchase_orders WHERE id = %s",
        (po_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["supplier_id"] != sid:
        raise HTTPException(status_code=403)
    if row["status"] not in ("drafted", "sent"):
        raise HTTPException(status_code=409, detail=f"当前状态 '{row['status']}' 不允许接单")
    db.execute("UPDATE material_purchase_orders SET status='accepted' WHERE id=%s", (po_id,))
    return {"status": "accepted"}


@router.post("/pos/{po_id}/ship")
async def ship_po(
    po_id: int,
    payload: ShipmentCreate,
    user: CurrentUser = Depends(_require_material),
):
    sid = _my_supplier_id(user)
    row = db.fetch_one(
        "SELECT id, status, supplier_id, qty FROM material_purchase_orders WHERE id = %s",
        (po_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["supplier_id"] != sid:
        raise HTTPException(status_code=403)
    if row["status"] not in ("accepted", "shipped"):
        raise HTTPException(status_code=409,
                            detail=f"当前状态 '{row['status']}' 不允许发货 (需要先接单)")

    now = datetime.utcnow()
    ship_no = f"SH-{now.strftime('%Y%m%d')}-{po_id:06d}-{int(now.timestamp()) % 1000:03d}"

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO material_shipments
                    (shipment_no, po_id, qty_shipped, batch_no, carrier, tracking_no,
                     photo_paths, shipped_at, status, remark)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'shipped', %s)
                RETURNING id
                """,
                (ship_no, po_id, payload.qty_shipped, payload.batch_no,
                 payload.carrier, payload.tracking_no,
                 payload.photo_paths, now, payload.remark),
            )
            sh_id = cur.fetchone()[0]

            cur.execute(
                "UPDATE material_purchase_orders SET status='shipped' WHERE id=%s",
                (po_id,),
            )

    return {"shipment_id": sh_id, "shipment_no": ship_no, "status": "shipped"}


@router.get("/shipments")
async def list_my_shipments(user: CurrentUser = Depends(_require_material)):
    sid = _my_supplier_id(user)
    if sid is None:
        return {"items": [], "total": 0}
    rows = db.fetch_all(
        """
        SELECT sh.*, mpo.po_no, mpo.material_code, mpo.spec,
               p.project_no, p.name AS project_name
        FROM material_shipments sh
        JOIN material_purchase_orders mpo ON sh.po_id = mpo.id
        JOIN projects p ON mpo.project_id = p.id
        WHERE mpo.supplier_id = %s
        ORDER BY sh.created_at DESC
        LIMIT 100
        """,
        (sid,),
    )
    for r in rows:
        if r.get("qty_shipped") is not None:
            r["qty_shipped"] = float(r["qty_shipped"])
    return {"items": rows, "total": len(rows)}



# =============================================================================
# 拍照取证：发货照片上传
# =============================================================================

from fastapi import UploadFile, File, Form
from pathlib import Path as _Path
import shutil as _shutil
import uuid as _uuid

_SHIPMENT_UPLOAD = _Path(__file__).resolve().parent.parent.parent / "uploads" / "shipments"
_SHIPMENT_UPLOAD.mkdir(parents=True, exist_ok=True)


@router.post("/pos/{po_id}/photos")
async def upload_shipment_photo(
    po_id: int,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_require_material),
):
    """发货前上传物料/包装照片。返回相对路径，由前端收集后 POST 到 /ship。"""
    sid = _my_supplier_id(user)
    po = db.fetch_one(
        "SELECT id, supplier_id FROM material_purchase_orders WHERE id=%s",
        (po_id,),
    )
    if po is None:
        raise HTTPException(status_code=404)
    if po["supplier_id"] != sid:
        raise HTTPException(status_code=403)

    ext = _Path(file.filename or "").suffix
    stored = f"po{po_id}_{_uuid.uuid4().hex[:8]}{ext}"
    target = _SHIPMENT_UPLOAD / stored
    with target.open("wb") as f:
        _shutil.copyfileobj(file.file, f)
    size = target.stat().st_size

    rel = f"shipments/{stored}"
    return {
        "path": rel,
        "url":  f"/uploads/{rel}",
        "file_name": file.filename,
        "file_size": size,
    }
