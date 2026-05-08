"""
Stage 5-QC — 质检 + 异常处理（我方视角）

设计原则：
  - 异常的唯一触发入口 = inspections.result='fail'
  - inspections 覆盖 4 种来源：material / internal_production / outsource / final
  - fail 时自动调 exception_rules.analyze_exception() 生成建议
  - 质检主管 (QUALITY_MANAGER) 确认责任方 + 选择处理路径 → 生成 rework_orders

API 列表：
  GET    /api/internal/qc/pending              待质检清单（按来源）
  POST   /api/internal/qc/inspections          录入质检结果（fail 自动触发异常）
  GET    /api/internal/qc/inspections          查询质检历史
  GET    /api/internal/qc/exceptions           我的异常清单
  GET    /api/internal/qc/exceptions/{id}      异常详情 + AI 报告
  POST   /api/internal/qc/exceptions/{id}/confirm   确认责任方 + 选处理路径
  POST   /api/internal/qc/exceptions/{id}/evidence  追加证据
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type
from mvp.exception_rules import analyze_exception

router = APIRouter(prefix="/api/internal/qc", tags=["qc"])
_require_internal = require_tenant_type("internal")


# =============================================================================
# Pydantic models
# =============================================================================

class InspectionCreate(BaseModel):
    project_id: int
    subject_type: str = Field(..., pattern="^(material|internal_production|outsource|final)$")
    subject_id: Optional[int] = None
    part_id: Optional[int] = None
    process_id: Optional[int] = None
    result: str = Field(..., pattern="^(pass|fail|concession)$")
    sample_qty: int = Field(..., ge=1)
    defect_qty: int = Field(0, ge=0)
    defect_type: Optional[str] = None
    photo_paths: Optional[list[str]] = None
    notes: Optional[str] = None


class ExceptionConfirm(BaseModel):
    responsible_party: str = Field(..., pattern="^(material_supplier|processor|internal|customer|shared)$")
    responsibility_ratio: float = Field(100.0, ge=0, le=100)
    resolution_path: str = Field(..., pattern="^(rework_material|rework_process|concession|claim)$")
    reason: Optional[str] = None


class EvidenceAppend(BaseModel):
    evidence_type: str      # drawing / inspection_report / photo / waybill / iqc_report / other
    attachment_id: Optional[int] = None
    description: Optional[str] = None


# =============================================================================
# 工具
# =============================================================================

def _gen_inspection_no() -> str:
    return f"INS-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

def _gen_exception_no(project_no: str) -> str:
    return f"QE-{project_no}-{uuid.uuid4().hex[:4].upper()}"

def _gen_rework_no(project_no: str) -> str:
    return f"RW-{project_no}-{uuid.uuid4().hex[:4].upper()}"


# =============================================================================
# 待质检清单
# =============================================================================

@router.get("/pending")
async def list_pending(user: CurrentUser = Depends(_require_internal)):
    """待质检：已交货但没质检过的 outsource_orders + material_shipments(已收货) + internal_production_orders(finished)。"""

    # 1) 外协已交货 & 尚未质检通过
    outsource = db.fetch_all(
        """
        SELECT o.id AS subject_id, 'outsource' AS subject_type,
               o.order_no, o.supplier_id, s.name AS supplier_name,
               o.quantity, o.delivered_at AS ready_at,
               p.id AS project_id, p.project_no, p.name AS project_name,
               r.title AS request_title
        FROM outsource_orders o
        JOIN suppliers s ON o.supplier_id = s.id
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE o.status = 'delivered'
          AND p.tenant_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM inspections i
              WHERE i.subject_type = 'outsource' AND i.subject_id = o.id
                AND i.result IN ('pass', 'concession')
          )
        ORDER BY o.delivered_at DESC
        LIMIT 50
        """,
        (user.tenant_id,),
    )

    # 2) 材料方已收货
    material = db.fetch_all(
        """
        SELECT mpo.id AS subject_id, 'material' AS subject_type,
               mpo.po_no AS order_no, mpo.supplier_id,
               s.name AS supplier_name,
               mpo.qty AS quantity, mpo.created_at AS ready_at,
               p.id AS project_id, p.project_no, p.name AS project_name,
               mpo.material_code AS request_title
        FROM material_purchase_orders mpo
        JOIN suppliers s ON mpo.supplier_id = s.id
        JOIN projects p ON mpo.project_id = p.id
        WHERE mpo.status = 'received'
          AND p.tenant_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM inspections i
              WHERE i.subject_type = 'material' AND i.subject_id = mpo.id
                AND i.result IN ('pass', 'concession')
          )
        ORDER BY mpo.updated_at DESC
        LIMIT 50
        """,
        (user.tenant_id,),
    )

    # 3) 自制已完工
    internal = db.fetch_all(
        """
        SELECT ipo.id AS subject_id, 'internal_production' AS subject_type,
               ipo.order_no, NULL AS supplier_id, NULL AS supplier_name,
               ipo.qty AS quantity, ipo.finished_at AS ready_at,
               p.id AS project_id, p.project_no, p.name AS project_name,
               pp.part_no AS request_title
        FROM internal_production_orders ipo
        LEFT JOIN project_parts pp ON ipo.part_id = pp.id
        JOIN projects p ON ipo.project_id = p.id
        WHERE ipo.status = 'finished'
          AND p.tenant_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM inspections i
              WHERE i.subject_type = 'internal_production' AND i.subject_id = ipo.id
                AND i.result IN ('pass', 'concession')
          )
        ORDER BY ipo.finished_at DESC
        LIMIT 50
        """,
        (user.tenant_id,),
    )

    items = [*outsource, *material, *internal]
    # 数量类型归一
    for x in items:
        if x.get("quantity") is not None:
            try:
                x["quantity"] = float(x["quantity"])
            except Exception:
                pass

    return {
        "items": items,
        "stats": {
            "outsource":            len(outsource),
            "material":             len(material),
            "internal_production":  len(internal),
            "total":                len(items),
        },
    }


# =============================================================================
# 录入质检
# =============================================================================

@router.post("/inspections", status_code=201)
async def create_inspection(
    payload: InspectionCreate,
    user: CurrentUser = Depends(_require_internal),
):
    # 校验项目归属
    proj = db.fetch_one(
        "SELECT id, project_no, name, tenant_id FROM projects WHERE id=%s",
        (payload.project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")

    insp_no = _gen_inspection_no()
    now = datetime.utcnow()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO inspections
                    (inspection_no, project_id, part_id, process_id,
                     subject_type, subject_id, inspector_id,
                     result, sample_qty, defect_qty, defect_type,
                     photo_paths, notes, inspected_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (insp_no, payload.project_id, payload.part_id, payload.process_id,
                 payload.subject_type, payload.subject_id, user.user_id,
                 payload.result, payload.sample_qty, payload.defect_qty,
                 payload.defect_type, payload.photo_paths, payload.notes, now),
            )
            inspection_id = cur.fetchone()[0]

            exception_id = None
            analysis_dict = None

            # fail → 自动创建异常 + 跑规则引擎
            if payload.result == "fail":
                analysis = analyze_exception(
                    defect_type=payload.defect_type,
                    sample_qty=payload.sample_qty,
                    defect_qty=payload.defect_qty,
                    subject_type=payload.subject_type,
                    notes=payload.notes,
                )
                analysis_dict = analysis.to_dict()
                exc_no = _gen_exception_no(proj["project_no"])

                cur.execute(
                    """
                    INSERT INTO quality_exceptions
                        (exception_no, inspection_id, project_id, part_id, process_id,
                         severity, exception_type, description,
                         ai_analysis_json, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ai_analyzed', %s)
                    RETURNING id
                    """,
                    (exc_no, inspection_id, payload.project_id,
                     payload.part_id, payload.process_id,
                     analysis.severity, payload.defect_type,
                     payload.notes or analysis.probable_cause,
                     json.dumps(analysis_dict, ensure_ascii=False),
                     user.user_id),
                )
                exception_id = cur.fetchone()[0]

                # 回填 inspection.exception_id
                cur.execute(
                    "UPDATE inspections SET exception_id=%s WHERE id=%s",
                    (exception_id, inspection_id),
                )

                # 自动挂一条 evidence（质检报告）
                cur.execute(
                    """
                    INSERT INTO exception_evidence
                        (exception_id, evidence_type, description, source, created_by)
                    VALUES (%s, 'inspection_report', %s, 'auto_collected', %s)
                    """,
                    (exception_id,
                     f"质检单 {insp_no}，不良 {payload.defect_qty}/{payload.sample_qty}",
                     user.user_id),
                )

    return {
        "inspection_id": inspection_id,
        "inspection_no": insp_no,
        "exception_id": exception_id,
        "ai_analysis": analysis_dict,
    }


@router.get("/inspections")
async def list_inspections(
    project_id: Optional[int] = None,
    result: Optional[str] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if project_id is not None:
        where += " AND i.project_id = %s"
        params.append(project_id)
    if result:
        where += " AND i.result = %s"
        params.append(result)

    rows = db.fetch_all(
        f"""
        SELECT i.*, p.project_no, p.name AS project_name,
               u.display_name AS inspector_name
        FROM inspections i
        JOIN projects p ON i.project_id = p.id
        LEFT JOIN users u ON i.inspector_id = u.id
        {where}
        ORDER BY i.inspected_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    return {"items": rows, "total": len(rows)}


# =============================================================================
# 异常清单
# =============================================================================

@router.get("/exceptions")
async def list_exceptions(
    status_filter: Optional[str] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if status_filter:
        where += " AND e.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT e.*, p.project_no, p.name AS project_name,
               pp.part_no, pr.process_code,
               i.inspection_no, i.defect_qty, i.sample_qty
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        LEFT JOIN project_parts pp ON e.part_id = pp.id
        LEFT JOIN project_processes pr ON e.process_id = pr.id
        LEFT JOIN inspections i ON e.inspection_id = i.id
        {where}
        ORDER BY e.created_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    # ai_analysis_json 给前端直接展开
    for r in rows:
        raw = r.get("ai_analysis_json")
        if raw and isinstance(raw, str):
            try: r["ai_analysis"] = json.loads(raw)
            except Exception: r["ai_analysis"] = None
        else:
            r["ai_analysis"] = raw
    return {"items": rows, "total": len(rows)}


@router.get("/exceptions/{exception_id}")
async def get_exception(
    exception_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        """
        SELECT e.*, p.project_no, p.name AS project_name, p.tenant_id,
               pp.part_no, pp.part_name, pr.process_code,
               i.inspection_no, i.defect_qty, i.sample_qty,
               i.defect_type AS insp_defect_type, i.notes AS insp_notes,
               i.subject_type AS insp_subject_type,
               i.subject_id AS insp_subject_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        LEFT JOIN project_parts pp ON e.part_id = pp.id
        LEFT JOIN project_processes pr ON e.process_id = pr.id
        LEFT JOIN inspections i ON e.inspection_id = i.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)

    # AI 报告
    raw = row.get("ai_analysis_json")
    row["ai_analysis"] = json.loads(raw) if isinstance(raw, str) and raw else raw
    row.pop("ai_analysis_json", None)

    # 证据
    evidence = db.fetch_all(
        """
        SELECT ee.*, a.file_name, u.display_name AS creator
        FROM exception_evidence ee
        LEFT JOIN attachments a ON ee.attachment_id = a.id
        LEFT JOIN users u ON ee.created_by = u.id
        WHERE ee.exception_id = %s
        ORDER BY ee.created_at ASC
        """,
        (exception_id,),
    )

    # 责任判定
    responsibility = db.fetch_all(
        """
        SELECT er.*, t.name AS responsible_tenant_name,
               u.display_name AS confirmed_by_name
        FROM exception_responsibility er
        LEFT JOIN tenants t ON er.responsible_tenant_id = t.id
        LEFT JOIN users u ON er.confirmed_by = u.id
        WHERE er.exception_id = %s
        """,
        (exception_id,),
    )
    for r in responsibility:
        if r.get("responsibility_ratio") is not None:
            r["responsibility_ratio"] = float(r["responsibility_ratio"])

    # 关联的 rework_orders
    rework = db.fetch_all(
        "SELECT * FROM rework_orders WHERE exception_id = %s ORDER BY created_at",
        (exception_id,),
    )
    for r in rework:
        if r.get("qty") is not None:
            r["qty"] = float(r["qty"])

    return {
        "exception": row,
        "evidence": evidence,
        "responsibility": responsibility,
        "rework_orders": rework,
    }


# =============================================================================
# 责任确认 + 生成 rework_orders
# =============================================================================

@router.post("/exceptions/{exception_id}/confirm")
async def confirm_exception(
    exception_id: int,
    payload: ExceptionConfirm,
    user: CurrentUser = Depends(_require_internal),
):
    """质检主管确认责任方和处理路径。
    路径 → 自动生成 rework_orders：
      - rework_material   : type='material' → 通知材料方补料
      - rework_process    : type='process'  → 通知加工方重工
      - concession        : 不生成 rework
      - claim             : type='process' + is_chargeable=TRUE
    """
    exc = db.fetch_one(
        """
        SELECT e.*, p.project_no, p.tenant_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if exc["status"] in ("resolved", "closed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"异常已 {exc['status']}，不可再改")

    # 方案 B：只要 internal 租户任一用户均可（_require_internal 已保证）

    now = datetime.utcnow()

    # 根据责任方找对应 tenant_id
    responsible_tenant_id = None
    if payload.responsible_party == "material_supplier":
        # 从原 inspection/subject 找材料方
        if exc["inspection_id"]:
            row = db.fetch_one(
                """
                SELECT mpo.tenant_id AS tid
                FROM inspections i
                JOIN material_purchase_orders mpo
                  ON i.subject_type='material' AND mpo.id = i.subject_id
                WHERE i.id = %s
                """,
                (exc["inspection_id"],),
            )
            responsible_tenant_id = (row or {}).get("tid")
    elif payload.responsible_party == "processor":
        if exc["inspection_id"]:
            row = db.fetch_one(
                """
                SELECT o.tenant_id AS tid
                FROM inspections i
                JOIN outsource_orders o
                  ON i.subject_type='outsource' AND o.id = i.subject_id
                WHERE i.id = %s
                """,
                (exc["inspection_id"],),
            )
            responsible_tenant_id = (row or {}).get("tid")

    rework_order_id = None
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1) 责任判定记录
            cur.execute(
                """
                INSERT INTO exception_responsibility
                    (exception_id, responsible_party, responsible_tenant_id,
                     responsibility_ratio, reason, ai_suggested,
                     confirmed_by, confirmed_at)
                VALUES (%s, %s, %s, %s, %s, FALSE, %s, %s)
                """,
                (exception_id, payload.responsible_party, responsible_tenant_id,
                 payload.responsibility_ratio, payload.reason,
                 user.user_id, now),
            )

            # 2) 更新异常状态
            cur.execute(
                """
                UPDATE quality_exceptions
                SET status = 'responsibility_confirmed',
                    resolution_path = %s,
                    confirmed_by = %s, confirmed_at = %s
                WHERE id = %s
                """,
                (payload.resolution_path, user.user_id, now, exception_id),
            )

            # 3) 生成 rework_order（除 concession 外）
            if payload.resolution_path in ("rework_material", "rework_process", "claim"):
                rw_no = _gen_rework_no(exc["project_no"])
                rw_type = "material" if payload.resolution_path == "rework_material" else "process"
                # 原单据关联
                orig_po_id = None
                orig_order_id = None
                if exc["inspection_id"]:
                    orig = db.fetch_one(
                        "SELECT subject_type, subject_id FROM inspections WHERE id=%s",
                        (exc["inspection_id"],),
                    )
                    if orig:
                        if orig["subject_type"] == "material":
                            orig_po_id = orig["subject_id"]
                        elif orig["subject_type"] == "outsource":
                            orig_order_id = orig["subject_id"]

                cur.execute(
                    """
                    INSERT INTO rework_orders
                        (rework_no, exception_id, project_id, rework_type,
                         original_po_id, original_order_id,
                         status, is_chargeable, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                    RETURNING id
                    """,
                    (rw_no, exception_id, exc["project_id"], rw_type,
                     orig_po_id, orig_order_id,
                     payload.resolution_path == "claim",
                     user.user_id),
                )
                rework_order_id = cur.fetchone()[0]

    return {
        "status": "responsibility_confirmed",
        "resolution_path": payload.resolution_path,
        "rework_order_id": rework_order_id,
    }


@router.post("/exceptions/{exception_id}/evidence", status_code=201)
async def append_evidence(
    exception_id: int,
    payload: EvidenceAppend,
    user: CurrentUser = Depends(_require_internal),
):
    exc = db.fetch_one(
        """
        SELECT e.id, p.tenant_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    new_id = db.execute(
        """
        INSERT INTO exception_evidence
            (exception_id, evidence_type, attachment_id, description, source, created_by)
        VALUES (%s, %s, %s, %s, 'manual_upload', %s)
        RETURNING id
        """,
        (exception_id, payload.evidence_type, payload.attachment_id,
         payload.description, user.user_id),
    )
    return {"id": new_id}



# =============================================================================
# 完善：上传证据文件、追加责任、变更路径、标记解决、对方视角
# =============================================================================

from fastapi import UploadFile, File, Form
import shutil
from pathlib import Path as _Path

_UPLOADS = _Path(__file__).resolve().parent.parent.parent / "uploads" / "exceptions"
_UPLOADS.mkdir(parents=True, exist_ok=True)


@router.post("/exceptions/{exception_id}/evidence/upload", status_code=201)
async def upload_evidence_file(
    exception_id: int,
    file: UploadFile = File(...),
    evidence_type: str = Form("photo"),
    description: str = Form(""),
    user: CurrentUser = Depends(_require_internal),
):
    """上传证据文件（图纸/照片/质检报告/运单）"""
    exc = db.fetch_one(
        """
        SELECT e.id, p.tenant_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    # save file
    ext = _Path(file.filename or "").suffix
    stored = f"{exception_id}_{uuid.uuid4().hex}{ext}"
    path = _UPLOADS / stored
    with path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    size = path.stat().st_size

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # attachments
            cur.execute(
                """
                INSERT INTO attachments
                    (related_type, related_id, file_name, file_path, file_size,
                     mime_type, uploaded_by, category)
                VALUES ('quality_exception', %s, %s, %s, %s, %s, %s, 'evidence')
                RETURNING id
                """,
                (exception_id, file.filename, f"exceptions/{stored}",
                 size, file.content_type, user.user_id),
            )
            att_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO exception_evidence
                    (exception_id, evidence_type, attachment_id, description,
                     source, created_by)
                VALUES (%s, %s, %s, %s, 'manual_upload', %s)
                RETURNING id
                """,
                (exception_id, evidence_type, att_id, description, user.user_id),
            )
            ee_id = cur.fetchone()[0]

    return {"evidence_id": ee_id, "attachment_id": att_id,
            "file_name": file.filename, "file_size": size}


class ResponsibilityAppend(BaseModel):
    responsible_party: str
    responsibility_ratio: float = Field(..., ge=0, le=100)
    responsible_tenant_id: Optional[int] = None
    reason: Optional[str] = None


@router.post("/exceptions/{exception_id}/responsibility")
async def append_responsibility(
    exception_id: int,
    payload: ResponsibilityAppend,
    user: CurrentUser = Depends(_require_internal),
):
    """追加一条责任记录（shared 模式下多方分摊）"""
    exc = db.fetch_one(
        """
        SELECT e.id, p.tenant_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    # 方案 B：tenant_type=internal 即可（_require_internal 已校验）

    new_id = db.execute(
        """
        INSERT INTO exception_responsibility
            (exception_id, responsible_party, responsible_tenant_id,
             responsibility_ratio, reason, ai_suggested,
             confirmed_by, confirmed_at)
        VALUES (%s, %s, %s, %s, %s, FALSE, %s, %s)
        RETURNING id
        """,
        (exception_id, payload.responsible_party, payload.responsible_tenant_id,
         payload.responsibility_ratio, payload.reason,
         user.user_id, datetime.utcnow()),
    )
    return {"id": new_id}


class PathChange(BaseModel):
    resolution_path: str = Field(..., pattern="^(rework_material|rework_process|concession|claim)$")
    reason: Optional[str] = None


@router.patch("/exceptions/{exception_id}/path")
async def change_resolution_path(
    exception_id: int,
    payload: PathChange,
    user: CurrentUser = Depends(_require_internal),
):
    """变更处理路径（如从 concession 升级 claim）"""
    exc = db.fetch_one(
        """
        SELECT e.*, p.tenant_id, p.project_no
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    # 方案 B：tenant_type=internal 即可
    if exc["status"] in ("resolved", "closed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"异常已 {exc['status']}，不可变更")

    old_path = exc["resolution_path"]
    new_path = payload.resolution_path

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quality_exceptions SET resolution_path=%s WHERE id=%s",
                (new_path, exception_id),
            )
            # 若原路径没生成 rework，新路径要生成
            if new_path in ("rework_material", "rework_process", "claim"):
                cur.execute(
                    "SELECT 1 FROM rework_orders WHERE exception_id=%s LIMIT 1",
                    (exception_id,),
                )
                if not cur.fetchone():
                    rw_no = _gen_rework_no(exc["project_no"])
                    rw_type = "material" if new_path == "rework_material" else "process"
                    cur.execute(
                        """
                        INSERT INTO rework_orders
                            (rework_no, exception_id, project_id, rework_type,
                             status, is_chargeable, created_by)
                        VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                        """,
                        (rw_no, exception_id, exc["project_id"], rw_type,
                         new_path == "claim", user.user_id),
                    )
    return {"old_path": old_path, "new_path": new_path}


@router.post("/exceptions/{exception_id}/resolve")
async def resolve_exception(
    exception_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """标记异常已解决（需所有 rework_order 都 completed 或 resolution=concession）"""
    exc = db.fetch_one(
        """
        SELECT e.*, p.tenant_id
        FROM quality_exceptions e
        JOIN projects p ON e.project_id = p.id
        WHERE e.id = %s
        """,
        (exception_id,),
    )
    if exc is None or exc["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    # 方案 B：tenant_type=internal 即可
    if exc["status"] != "responsibility_confirmed":
        raise HTTPException(status_code=409,
                            detail=f"当前 '{exc['status']}' 不能解决，需先 responsibility_confirmed")

    # 校验 rework_orders
    if exc["resolution_path"] in ("rework_material", "rework_process", "claim"):
        rw = db.fetch_all(
            "SELECT status FROM rework_orders WHERE exception_id=%s",
            (exception_id,),
        )
        if rw and any(r["status"] not in ("completed","cancelled") for r in rw):
            raise HTTPException(status_code=409,
                                detail="仍有 rework_orders 未完成")

    db.execute(
        """
        UPDATE quality_exceptions
        SET status='resolved', resolved_at=%s
        WHERE id=%s
        """,
        (datetime.utcnow(), exception_id),
    )
    return {"status": "resolved"}


class ReworkStatus(BaseModel):
    status: str = Field(..., pattern="^(in_progress|completed|cancelled)$")
    remark: Optional[str] = None


@router.patch("/rework-orders/{rework_id}/status")
async def update_rework_status(
    rework_id: int,
    payload: ReworkStatus,
    user: CurrentUser = Depends(_require_internal),
):
    """推进补料/重工单的状态"""
    row = db.fetch_one(
        """
        SELECT rw.id, p.tenant_id
        FROM rework_orders rw
        JOIN projects p ON rw.project_id = p.id
        WHERE rw.id = %s
        """,
        (rework_id,),
    )
    if row is None or row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    db.execute(
        "UPDATE rework_orders SET status=%s WHERE id=%s",
        (payload.status, rework_id),
    )
    return {"status": payload.status}


# =============================================================================
# 对方视角：加工方 + 材料方看自己被判责的异常
# =============================================================================

_counterparty_router = APIRouter(prefix="/api", tags=["qc-counterparty"])


@_counterparty_router.get("/processor/my-exceptions")
async def my_exceptions_processor(user: CurrentUser = Depends(require_tenant_type("processor"))):
    return _list_counterparty_exceptions(user)


@_counterparty_router.get("/material/my-exceptions")
async def my_exceptions_material(user: CurrentUser = Depends(require_tenant_type("material"))):
    return _list_counterparty_exceptions(user)


def _list_counterparty_exceptions(user: CurrentUser):
    """返回所有判定本租户承担责任的异常，以及对应的 rework_orders 通知。"""
    rows = db.fetch_all(
        """
        SELECT e.id, e.exception_no, e.severity, e.exception_type, e.status,
               e.resolution_path, e.description, e.created_at,
               er.responsibility_ratio,
               p.project_no, p.name AS project_name
        FROM quality_exceptions e
        JOIN exception_responsibility er ON er.exception_id = e.id
        JOIN projects p ON e.project_id = p.id
        WHERE er.responsible_tenant_id = %s
        ORDER BY e.created_at DESC
        """,
        (user.tenant_id,),
    )
    for r in rows:
        if r.get("responsibility_ratio") is not None:
            r["responsibility_ratio"] = float(r["responsibility_ratio"])

    # 附带 rework_orders 汇总
    for r in rows:
        rw = db.fetch_all(
            "SELECT id, rework_no, rework_type, status FROM rework_orders WHERE exception_id=%s",
            (r["id"],),
        )
        r["rework_orders"] = rw
    return {"items": rows, "total": len(rows)}


# 注册到外部（需要在 main.py include）
def get_counterparty_router():
    return _counterparty_router
