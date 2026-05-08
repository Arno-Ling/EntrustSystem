"""
Stage 6-RECON — 对账 + 异常扣减

按（供应商 + 期间）聚合应付和异常扣减：
  - 应付 CHARGE：
      * 外协加工单 (outsource_orders.status='delivered')  → 供应商=加工方
      * 材料采购单 (material_purchase_orders.status='received') → 供应商=材料方
  - 扣减 DEDUCTION：
      * rework_orders.is_chargeable=TRUE 关联的异常，按 responsibility_ratio 扣
      * （MVP 简化：扣减金额 = 原单件价 * 不良数量 * 比例）

API：
  POST /api/internal/recon/generate   按 tenant_id + period 生成草稿（幂等 UPSERT）
  GET  /api/internal/recon             我方看全部
  GET  /api/internal/recon/{id}        详情（主表 + items）
  POST /api/internal/recon/{id}/submit 推送对方确认
  POST /api/internal/recon/{id}/settle 标记已结算

对方视角：
  GET  /api/material/recon   材料方看自己的对账单
  GET  /api/processor/recon  加工方看自己的对账单
  POST /api/material/recon/{id}/confirm   确认对账
  POST /api/processor/recon/{id}/confirm  确认对账
"""
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

internal_router  = APIRouter(prefix="/api/internal/recon", tags=["recon-internal"])
material_router  = APIRouter(prefix="/api/material/recon", tags=["recon-material"])
processor_router = APIRouter(prefix="/api/processor/recon", tags=["recon-processor"])

_require_internal  = require_tenant_type("internal")
_require_material  = require_tenant_type("material")
_require_processor = require_tenant_type("processor")


# =============================================================================
# Pydantic
# =============================================================================

class ReconGenerate(BaseModel):
    tenant_id: int                   # 加工方或材料方租户
    period_from: str                 # YYYY-MM-DD
    period_to: str                   # YYYY-MM-DD


class ReconConfirm(BaseModel):
    accept: bool = True
    note: Optional[str] = None


# =============================================================================
# 我方：生成对账单
# =============================================================================

def _gen_recon_no(tenant_id: int, period_from: str) -> str:
    return f"RC-{period_from.replace('-', '')[:6]}-T{tenant_id:03d}"


@internal_router.post("/generate")
async def generate_recon(
    payload: ReconGenerate,
    user: CurrentUser = Depends(_require_internal),
):
    # 1) 取目标租户信息
    t = db.fetch_one(
        "SELECT id, name, tenant_type, supplier_id FROM tenants WHERE id=%s",
        (payload.tenant_id,),
    )
    if t is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if t["tenant_type"] not in ("material", "processor"):
        raise HTTPException(status_code=400, detail="对账仅对材料方/加工方租户")
    supplier_id = t["supplier_id"]
    if supplier_id is None:
        raise HTTPException(status_code=400, detail="该租户未关联 suppliers.id，无法对账")

    # 2) 聚合 charges（本期应付）
    charges: list[dict] = []

    if t["tenant_type"] == "processor":
        # 外协加工单
        rows = db.fetch_all(
            """
            SELECT o.id AS src_id, o.order_no,
                   o.unit_price * o.quantity AS amount,
                   o.delivered_at,
                   r.title AS note
            FROM outsource_orders o
            JOIN outsource_requests r ON o.request_id = r.id
            WHERE o.supplier_id = %s
              AND o.status IN ('delivered', 'accepted', 'producing')
              AND o.delivered_at IS NOT NULL
              AND o.delivered_at::date BETWEEN %s AND %s
            """,
            (supplier_id, payload.period_from, payload.period_to),
        )
        for r in rows:
            charges.append({
                "source_type": "outsource_order",
                "source_id":   r["src_id"],
                "description": f"外协加工单 {r['order_no']} · {r['note']}",
                "amount":      float(r["amount"] or 0),
                "related_exception_id": None,
            })

    else:  # material
        rows = db.fetch_all(
            """
            SELECT mpo.id AS src_id, mpo.po_no,
                   mpo.total_amount AS amount,
                   mpo.updated_at,
                   mpo.material_code AS note
            FROM material_purchase_orders mpo
            WHERE mpo.supplier_id = %s
              AND mpo.status IN ('received', 'shipped')
              AND mpo.updated_at::date BETWEEN %s AND %s
              AND mpo.total_amount IS NOT NULL
            """,
            (supplier_id, payload.period_from, payload.period_to),
        )
        for r in rows:
            charges.append({
                "source_type": "material_po",
                "source_id":   r["src_id"],
                "description": f"材料采购单 {r['po_no']} · {r['note']}",
                "amount":      float(r["amount"] or 0),
                "related_exception_id": None,
            })

    # 3) 聚合 deductions（异常扣减）
    deductions: list[dict] = []
    rw_rows = db.fetch_all(
        """
        SELECT rw.id AS rw_id, rw.rework_no, rw.rework_type,
               rw.original_order_id, rw.original_po_id, rw.is_chargeable,
               rw.exception_id,
               er.responsibility_ratio,
               er.responsible_party,
               er.responsible_tenant_id
        FROM rework_orders rw
        LEFT JOIN exception_responsibility er
               ON er.exception_id = rw.exception_id
              AND er.responsible_tenant_id = %s
        WHERE rw.is_chargeable = TRUE
          AND rw.created_at::date BETWEEN %s AND %s
          AND er.id IS NOT NULL
        """,
        (payload.tenant_id, payload.period_from, payload.period_to),
    )
    for rw in rw_rows:
        # 估算扣减金额：取对应原单金额 * 比例 * (不良数 / 样本数) 的简化公式
        amount = 0.0
        if rw["original_order_id"]:
            row = db.fetch_one(
                "SELECT unit_price*quantity AS a FROM outsource_orders WHERE id=%s",
                (rw["original_order_id"],),
            )
            if row and row["a"]:
                amount = float(row["a"])
        elif rw["original_po_id"]:
            row = db.fetch_one(
                "SELECT total_amount AS a FROM material_purchase_orders WHERE id=%s",
                (rw["original_po_id"],),
            )
            if row and row["a"]:
                amount = float(row["a"])

        # 按不良率调整
        insp = db.fetch_one(
            """
            SELECT sample_qty, defect_qty
            FROM inspections
            WHERE exception_id = %s
            LIMIT 1
            """,
            (rw["exception_id"],),
        )
        defect_ratio = 1.0
        if insp and insp["sample_qty"] and insp["defect_qty"]:
            defect_ratio = min(1.0, insp["defect_qty"] / insp["sample_qty"])
        resp_ratio = float(rw["responsibility_ratio"] or 100) / 100

        ded_amount = round(amount * defect_ratio * resp_ratio, 2)

        deductions.append({
            "source_type": "rework_order",
            "source_id":   rw["rw_id"],
            "description": f"异常扣减 {rw['rework_no']} (责任比例 {rw['responsibility_ratio']}%)",
            "amount":      ded_amount,
            "related_exception_id": rw["exception_id"],
        })

    total_charge    = sum(c["amount"] for c in charges)
    total_deduction = sum(d["amount"] for d in deductions)
    net             = total_charge - total_deduction

    # 4) 幂等：存在同 recon_no 则覆盖
    recon_no = _gen_recon_no(payload.tenant_id, payload.period_from)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM reconciliations WHERE recon_no = %s", (recon_no,))
            existing = cur.fetchone()
            if existing:
                rec_id = existing[0]
                cur.execute("DELETE FROM reconciliation_items WHERE reconciliation_id = %s", (rec_id,))
                cur.execute(
                    """
                    UPDATE reconciliations
                    SET tenant_id=%s, period_from=%s, period_to=%s,
                        total_amount=%s, deduction_amount=%s, net_amount=%s,
                        status='drafted', confirmed_at=NULL, settled_at=NULL,
                        created_by=%s
                    WHERE id=%s
                    """,
                    (payload.tenant_id, payload.period_from, payload.period_to,
                     total_charge, total_deduction, net, user.user_id, rec_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO reconciliations
                        (recon_no, tenant_id, period_from, period_to,
                         total_amount, deduction_amount, net_amount,
                         status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'drafted', %s)
                    RETURNING id
                    """,
                    (recon_no, payload.tenant_id, payload.period_from, payload.period_to,
                     total_charge, total_deduction, net, user.user_id),
                )
                rec_id = cur.fetchone()[0]

            # items
            for c in charges:
                cur.execute(
                    """
                    INSERT INTO reconciliation_items
                        (reconciliation_id, source_type, source_id,
                         item_type, description, amount, related_exception_id)
                    VALUES (%s, %s, %s, 'charge', %s, %s, %s)
                    """,
                    (rec_id, c["source_type"], c["source_id"],
                     c["description"], c["amount"], c["related_exception_id"]),
                )
            for d in deductions:
                cur.execute(
                    """
                    INSERT INTO reconciliation_items
                        (reconciliation_id, source_type, source_id,
                         item_type, description, amount, related_exception_id)
                    VALUES (%s, %s, %s, 'deduction', %s, %s, %s)
                    """,
                    (rec_id, d["source_type"], d["source_id"],
                     d["description"], d["amount"], d["related_exception_id"]),
                )

    return {
        "id": rec_id,
        "recon_no": recon_no,
        "total_amount": total_charge,
        "deduction_amount": total_deduction,
        "net_amount": net,
        "charge_count": len(charges),
        "deduction_count": len(deductions),
    }


@internal_router.get("")
async def list_recons(
    tenant_type: Optional[str] = None,
    status_filter: Optional[str] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = ["1=1"]
    params: list[Any] = []
    if tenant_type:
        where.append("t.tenant_type = %s")
        params.append(tenant_type)
    if status_filter:
        where.append("r.status = %s")
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT r.*, t.name AS tenant_name, t.tenant_type
        FROM reconciliations r
        JOIN tenants t ON r.tenant_id = t.id
        WHERE {' AND '.join(where)}
        ORDER BY r.created_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        for k in ("total_amount","deduction_amount","net_amount"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"items": rows, "total": len(rows)}


@internal_router.get("/{rec_id}")
async def get_recon(rec_id: int, user: CurrentUser = Depends(_require_internal)):
    return _get_recon_detail(rec_id)


def _get_recon_detail(rec_id: int) -> dict:
    main = db.fetch_one(
        """
        SELECT r.*, t.name AS tenant_name, t.tenant_type
        FROM reconciliations r
        JOIN tenants t ON r.tenant_id = t.id
        WHERE r.id = %s
        """,
        (rec_id,),
    )
    if main is None:
        raise HTTPException(status_code=404)
    for k in ("total_amount","deduction_amount","net_amount"):
        if main.get(k) is not None:
            main[k] = float(main[k])

    items = db.fetch_all(
        "SELECT * FROM reconciliation_items WHERE reconciliation_id = %s ORDER BY item_type DESC, id",
        (rec_id,),
    )
    for i in items:
        if i.get("amount") is not None:
            i["amount"] = float(i["amount"])

    return {"recon": main, "items": items}


@internal_router.post("/{rec_id}/submit")
async def submit_recon(rec_id: int, user: CurrentUser = Depends(_require_internal)):
    row = db.fetch_one("SELECT status FROM reconciliations WHERE id=%s", (rec_id,))
    if row is None: raise HTTPException(status_code=404)
    if row["status"] != "drafted":
        raise HTTPException(status_code=409, detail=f"当前 '{row['status']}' 不可推送")
    db.execute("UPDATE reconciliations SET status='submitted' WHERE id=%s", (rec_id,))
    return {"status": "submitted"}


@internal_router.post("/{rec_id}/settle")
async def settle_recon(rec_id: int, user: CurrentUser = Depends(_require_internal)):
    row = db.fetch_one("SELECT status FROM reconciliations WHERE id=%s", (rec_id,))
    if row is None: raise HTTPException(status_code=404)
    if row["status"] not in ("confirmed",):
        raise HTTPException(status_code=409, detail=f"当前 '{row['status']}' 不可结算")
    db.execute(
        "UPDATE reconciliations SET status='settled', settled_at=%s WHERE id=%s",
        (datetime.utcnow(), rec_id),
    )
    return {"status": "settled"}


# =============================================================================
# 材料方 + 加工方：看自己的对账单
# =============================================================================

def _list_for_counterparty(user: CurrentUser, tenant_type: str):
    rows = db.fetch_all(
        """
        SELECT r.*, t.name AS tenant_name
        FROM reconciliations r
        JOIN tenants t ON r.tenant_id = t.id
        WHERE r.tenant_id = %s AND r.status IN ('submitted','confirmed','disputed','settled')
        ORDER BY r.created_at DESC
        """,
        (user.tenant_id,),
    )
    for r in rows:
        for k in ("total_amount","deduction_amount","net_amount"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"items": rows, "total": len(rows)}


@material_router.get("")
async def list_my_recons_material(user: CurrentUser = Depends(_require_material)):
    return _list_for_counterparty(user, "material")


@material_router.get("/{rec_id}")
async def get_my_recon_material(rec_id: int, user: CurrentUser = Depends(_require_material)):
    d = _get_recon_detail(rec_id)
    if d["recon"]["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    return d


@material_router.post("/{rec_id}/confirm")
async def confirm_recon_material(
    rec_id: int,
    payload: ReconConfirm,
    user: CurrentUser = Depends(_require_material),
):
    return _confirm_counterparty(rec_id, user, payload)


@processor_router.get("")
async def list_my_recons_processor(user: CurrentUser = Depends(_require_processor)):
    return _list_for_counterparty(user, "processor")


@processor_router.get("/{rec_id}")
async def get_my_recon_processor(rec_id: int, user: CurrentUser = Depends(_require_processor)):
    d = _get_recon_detail(rec_id)
    if d["recon"]["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    return d


@processor_router.post("/{rec_id}/confirm")
async def confirm_recon_processor(
    rec_id: int,
    payload: ReconConfirm,
    user: CurrentUser = Depends(_require_processor),
):
    return _confirm_counterparty(rec_id, user, payload)


def _confirm_counterparty(rec_id: int, user: CurrentUser, payload: ReconConfirm):
    row = db.fetch_one(
        "SELECT tenant_id, status FROM reconciliations WHERE id=%s",
        (rec_id,),
    )
    if row is None: raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if row["status"] != "submitted":
        raise HTTPException(status_code=409, detail=f"当前 '{row['status']}' 不可确认")

    new_status = "confirmed" if payload.accept else "disputed"
    db.execute(
        """
        UPDATE reconciliations
        SET status = %s, confirmed_at = %s,
            remark = COALESCE(%s, remark)
        WHERE id = %s
        """,
        (new_status, datetime.utcnow() if payload.accept else None,
         payload.note, rec_id),
    )
    return {"status": new_status}
