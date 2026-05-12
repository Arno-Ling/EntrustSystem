"""
Internal tenant routes (我方视角).

Stage 0 - projects CRUD + parts + attachments + confirm.
Stage 1 will extend with production_decisions + approvals.
Stage 3 will extend with outsource requests/orders.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from mvp import db
from mvp.auth import CurrentUser, require_tenant_type

router = APIRouter(prefix="/api/internal", tags=["internal"])

_require_internal = require_tenant_type("internal")

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
DRAWINGS_DIR = UPLOADS_DIR / "drawings"
DRAWINGS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    customer: str = Field(..., min_length=1, max_length=255)
    deadline: Optional[str] = None                       # YYYY-MM-DD
    unit_price: Optional[float] = None
    quantity: Optional[int] = None
    description: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    customer: Optional[str] = None
    deadline: Optional[str] = None
    unit_price: Optional[float] = None
    quantity: Optional[int] = None
    description: Optional[str] = None


class PartCreate(BaseModel):
    part_no: Optional[str] = None                   # 可省略，由后端根据 part_type + sort_no 生成 die-02 这种
    part_name: Optional[str] = None
    material: Optional[str] = None
    material_id: Optional[int] = None               # FK to materials 表（单选）
    qty: int = Field(default=1, ge=1)
    processes: list[str] = Field(default_factory=list)
    process_method_ids: Optional[list[int]] = None  # FK list to process_methods（多选）
    spec: Optional[str] = None
    mold_id: Optional[int] = None                   # 归属模具套
    part_type: Optional[str] = None                 # die / ins / part / frame / std
    sort_no: Optional[int] = None                   # 模具内排序


class MoldCreate(BaseModel):
    name: Optional[str] = None
    remark: Optional[str] = None
    sort_no: Optional[int] = None


class ProcessCreate(BaseModel):
    """创建/更新一道工序。工序号由后端按 P<seq>-<method>[-<faces>] 生成。"""
    method_name: str = Field(..., min_length=1, max_length=32)
    faces: Optional[str] = None                       # "A,B,C" / None
    notes: Optional[str] = None
    est_hours: Optional[float] = None
    seq_no: Optional[int] = None                      # 省略则自动追加为下一个


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

# 租户前缀字典：tenant name → 项目号首字母
# 瑞利捷 M  / 其他客户可以追加；未命中取名字首字的拼音首字母或 'X'
_TENANT_PREFIX_BY_NAME = {
    "我方-模具制造厂":   "M",   # 瑞利捷模具（MVP 内部租户）
    "瑞利捷":           "M",
}


def _tenant_prefix(tenant_name: str | None) -> str:
    if not tenant_name:
        return "X"
    # 精确匹配
    if tenant_name in _TENANT_PREFIX_BY_NAME:
        return _TENANT_PREFIX_BY_NAME[tenant_name]
    # 模糊匹配：名字里包含关键字
    for key, prefix in _TENANT_PREFIX_BY_NAME.items():
        if key in tenant_name:
            return prefix
    # 回退：取 ASCII 首字母，或中文第一字的 fallback
    for ch in tenant_name:
        if ch.isascii() and ch.isalpha():
            return ch.upper()
    return "X"


def _next_project_no(tenant_id: int, tenant_name: str | None) -> tuple[str, int, str]:
    """生成项目编号：<租户前缀><YY>-<4位序号>，例 M26-0013。
    租户内 年内 序号从 1 开始累加。返回 (project_no, tenant_seq, year_suffix)。
    """
    prefix = _tenant_prefix(tenant_name)
    year_suffix = datetime.now().strftime("%y")   # 例如 '26'

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(MAX(tenant_seq), 0)
                FROM projects
                WHERE tenant_id = %s AND year_suffix = %s
                """,
                (tenant_id, year_suffix),
            )
            max_seq = cur.fetchone()[0] or 0

    seq = max_seq + 1
    return f"{prefix}{year_suffix}-{seq:04d}", seq, year_suffix


def _gen_project_no() -> str:
    """[legacy] Kept for any caller; new flow uses _next_project_no()."""
    return f"PRJ-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


@router.get("/projects")
async def list_projects(
    user: CurrentUser = Depends(_require_internal),
    status_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    offset = max(0, (page - 1) * page_size)
    where = "WHERE tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT id, project_no, name, customer, deadline, unit_price, quantity,
               status, created_at, updated_at
        FROM projects {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (*params, page_size, offset),
    )
    total_row = db.fetch_one(
        f"SELECT COUNT(*) AS cnt FROM projects {where}",
        tuple(params),
    )
    return {
        "items": rows,
        "total": total_row["cnt"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@router.post("/projects", status_code=201)
async def create_project(
    payload: ProjectCreate,
    user: CurrentUser = Depends(_require_internal),
):
    # 生成租户内 年内 自增项目号：例 M26-0013
    project_no, tenant_seq, year_suffix = _next_project_no(user.tenant_id, user.tenant_name)
    new_id = db.execute(
        """
        INSERT INTO projects (tenant_id, project_no, tenant_seq, year_suffix,
                              name, customer, deadline,
                              unit_price, quantity, description, status, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'drafted', %s)
        RETURNING id
        """,
        (user.tenant_id, project_no, tenant_seq, year_suffix,
         payload.name, payload.customer,
         payload.deadline, payload.unit_price, payload.quantity,
         payload.description, user.user_id),
    )
    return {"id": new_id, "project_no": project_no, "status": "drafted"}


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    project = db.fetch_one(
        """
        SELECT id, project_no, name, customer, deadline, unit_price, quantity,
               description, status, created_by, confirmed_at, created_at, updated_at
        FROM projects WHERE id = %s AND tenant_id = %s
        """,
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    parts = db.fetch_all(
        """
        SELECT id, mold_id, part_type, sort_no,
               part_no, part_name, material, qty, processes_json, spec,
               created_at
        FROM project_parts WHERE project_id = %s
        ORDER BY COALESCE(mold_id, 0), sort_no NULLS LAST, id ASC
        """,
        (project_id,),
    )
    for p in parts:
        raw = p.get("processes_json")
        p["processes"] = (
            json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
        )
        p.pop("processes_json", None)

        # 新：从 project_processes 返回结构化工序
        proc_rows = db.fetch_all(
            """
            SELECT pp.id, pp.seq_no, pp.process_code, pp.method_name,
                   pp.faces, pp.notes, pp.est_hours,
                   pm.is_internal_capable, pm.category
            FROM project_processes pp
            LEFT JOIN process_methods pm ON pm.name = pp.method_name
            WHERE pp.part_id = %s
            ORDER BY pp.seq_no ASC
            """,
            (p["id"],),
        )
        for pr in proc_rows:
            if pr.get("est_hours") is not None:
                pr["est_hours"] = float(pr["est_hours"])
        p["process_list"] = proc_rows

    attachments = db.fetch_all(
        """
        SELECT id, file_name, file_path, file_size, mime_type, category, created_at
        FROM attachments
        WHERE related_type = 'project' AND related_id = %s
        ORDER BY created_at DESC
        """,
        (project_id,),
    )

    molds = db.fetch_all(
        """
        SELECT m.id, m.mold_no, m.name, m.sort_no, m.status, m.remark, m.created_at,
               (SELECT COUNT(*) FROM project_parts pp WHERE pp.mold_id = m.id) AS part_count
        FROM molds m WHERE m.project_id = %s
        ORDER BY m.sort_no ASC NULLS LAST, m.id ASC
        """,
        (project_id,),
    )

    # 图纸库关联
    drawing_links = db.fetch_all(
        """
        SELECT pdl.id AS link_id, pdl.drawing_id, pdl.linked_at,
               dl.name, dl.file_name, dl.category, dl.file_size, dl.mime_type
        FROM project_drawing_links pdl
        JOIN drawing_library dl ON pdl.drawing_id = dl.id
        WHERE pdl.project_id = %s
        ORDER BY pdl.linked_at DESC
        """,
        (project_id,),
    )

    return {
        "project": project,
        "molds": molds,
        "parts": parts,
        "attachments": attachments,
        "drawing_links": drawing_links,
    }


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    user: CurrentUser = Depends(_require_internal),
):
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] not in ("drafted",):
        raise HTTPException(status_code=409,
                            detail=f"Cannot edit project in status '{project['status']}'")

    updates: list[str] = []
    params: list[Any] = []
    for field, value in payload.model_dump(exclude_unset=True).items():
        updates.append(f"{field} = %s")
        params.append(value)
    if not updates:
        return {"updated": False}

    params.extend([project_id, user.tenant_id])
    db.execute(
        f"UPDATE projects SET {', '.join(updates)} "
        f"WHERE id = %s AND tenant_id = %s",
        tuple(params),
    )
    return {"updated": True}


@router.post("/projects/{project_id}/parts", status_code=201)
async def add_part(
    project_id: int,
    payload: PartCreate,
    user: CurrentUser = Depends(_require_internal),
):
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] != "drafted":
        raise HTTPException(status_code=409,
                            detail="Only 'drafted' projects can have parts added")

    # 校验 mold_id 归属
    mold_id = payload.mold_id
    if mold_id is not None:
        m = db.fetch_one(
            "SELECT id FROM molds WHERE id = %s AND project_id = %s",
            (mold_id, project_id),
        )
        if m is None:
            raise HTTPException(status_code=400, detail="mold_id 不属于该项目")

    # 自动生成零件号：<part_type>-<sort_no:02d>，例 die-02
    part_type = payload.part_type
    sort_no   = payload.sort_no
    part_no   = payload.part_no

    if not part_no:
        if not part_type:
            raise HTTPException(status_code=400, detail="part_no 或 part_type+sort_no 至少提供一个")
        if sort_no is None:
            # 同模具 + 同 part_type 下自增
            scope_mold = mold_id if mold_id is not None else None
            row = db.fetch_one(
                """
                SELECT COALESCE(MAX(sort_no), 0) AS mx
                FROM project_parts
                WHERE project_id = %s
                  AND COALESCE(mold_id, 0) = COALESCE(%s, 0)
                  AND part_type = %s
                """,
                (project_id, scope_mold, part_type),
            )
            sort_no = (row["mx"] or 0) + 1
        part_no = f"{part_type}-{sort_no:02d}"

    # 材质处理：material_id 优先于 material 文本
    material_text = payload.material
    material_id_val = payload.material_id
    if material_id_val is not None:
        mat_row = db.fetch_one(
            "SELECT id, code FROM materials WHERE id = %s",
            (material_id_val,),
        )
        if mat_row is None:
            raise HTTPException(status_code=400,
                                detail=f"material_id 不存在: {material_id_val}")
        material_text = mat_row["code"]  # 自动填充 material 文本列

    try:
        new_id = db.execute(
            """
            INSERT INTO project_parts (project_id, mold_id, part_type, sort_no,
                                       part_no, part_name, material, material_id,
                                       qty, processes_json, spec)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, mold_id, part_type, sort_no,
             part_no, payload.part_name, material_text, material_id_val,
             payload.qty, json.dumps(payload.processes, ensure_ascii=False),
             payload.spec),
        )
    except Exception as e:
        if "part_no" in str(e):
            raise HTTPException(status_code=409, detail=f"零件号冲突：{part_no}")
        raise

    # 工序处理：process_method_ids 优先于 processes 文本
    if payload.process_method_ids:
        # 校验所有 ID 存在
        placeholders = ",".join(["%s"] * len(payload.process_method_ids))
        existing = db.fetch_all(
            f"SELECT name FROM process_methods WHERE id IN ({placeholders})",
            tuple(payload.process_method_ids),
        )
        if len(existing) != len(payload.process_method_ids):
            existing_names = {r["name"] for r in existing}
            # 需要用 id 查找无效的
            all_methods = db.fetch_all("SELECT id, name FROM process_methods")
            valid_ids = {r["id"] for r in all_methods}
            invalid = [mid for mid in payload.process_method_ids if mid not in valid_ids]
            raise HTTPException(status_code=400,
                                detail=f"无效的工序ID: {invalid}")

        # 创建 project_processes 记录
        method_map = {r["id"]: r["name"] for r in db.fetch_all("SELECT id, name FROM process_methods")}
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                for seq, mid in enumerate(payload.process_method_ids, start=1):
                    method_name = method_map.get(mid, "")
                    process_code = f"P{seq}-{method_name}"
                    cur.execute(
                        """
                        INSERT INTO project_processes
                            (project_id, part_id, seq_no, process_code, method_name)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (project_id, new_id, seq, process_code, method_name),
                    )

    return {"id": new_id, "part_no": part_no, "sort_no": sort_no, "mold_id": mold_id}


# ---------------------------------------------------------------------------
# 模具套 — Molds
# ---------------------------------------------------------------------------

def _next_mold_no(project_id: int) -> tuple[str, int]:
    """生成 <project_no>-M<2位> 形式的模具号。"""
    proj = db.fetch_one("SELECT project_no FROM projects WHERE id = %s", (project_id,))
    row = db.fetch_one(
        "SELECT COALESCE(MAX(sort_no), 0) AS mx FROM molds WHERE project_id = %s",
        (project_id,),
    )
    sort_no = (row["mx"] or 0) + 1
    project_no = proj["project_no"] if proj else f"P{project_id}"
    return f"{project_no}-M{sort_no:02d}", sort_no


@router.get("/projects/{project_id}/molds")
async def list_molds(project_id: int, user: CurrentUser = Depends(_require_internal)):
    p = db.fetch_one("SELECT id FROM projects WHERE id=%s AND tenant_id=%s",
                     (project_id, user.tenant_id))
    if p is None:
        raise HTTPException(status_code=404)
    rows = db.fetch_all(
        """
        SELECT m.id, m.mold_no, m.name, m.sort_no, m.status, m.remark,
               m.created_at,
               (SELECT COUNT(*) FROM project_parts pp WHERE pp.mold_id = m.id) AS part_count
        FROM molds m
        WHERE m.project_id = %s
        ORDER BY m.sort_no ASC NULLS LAST, m.id ASC
        """,
        (project_id,),
    )
    return {"items": rows, "total": len(rows)}


@router.post("/projects/{project_id}/molds", status_code=201)
async def create_mold(
    project_id: int,
    payload: MoldCreate,
    user: CurrentUser = Depends(_require_internal),
):
    p = db.fetch_one("SELECT id, status FROM projects WHERE id=%s AND tenant_id=%s",
                     (project_id, user.tenant_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if p["status"] != "drafted":
        raise HTTPException(status_code=409, detail="只有 drafted 项目可以添加模具")

    if payload.sort_no is None:
        mold_no, sort_no = _next_mold_no(project_id)
    else:
        sort_no = payload.sort_no
        proj = db.fetch_one("SELECT project_no FROM projects WHERE id=%s", (project_id,))
        mold_no = f"{proj['project_no']}-M{sort_no:02d}"

    try:
        new_id = db.execute(
            """
            INSERT INTO molds (project_id, mold_no, name, sort_no, remark)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, mold_no, payload.name, sort_no, payload.remark),
        )
    except Exception as e:
        if "mold_no" in str(e) or "uk_mold_project_sort" in str(e):
            raise HTTPException(status_code=409, detail=f"模具编号/排序冲突：{mold_no}")
        raise
    return {"id": new_id, "mold_no": mold_no, "sort_no": sort_no}


@router.delete("/molds/{mold_id}")
async def delete_mold(mold_id: int, user: CurrentUser = Depends(_require_internal)):
    row = db.fetch_one(
        """
        SELECT m.id, p.status, p.tenant_id,
               (SELECT COUNT(*) FROM project_parts pp WHERE pp.mold_id = m.id) AS part_count
        FROM molds m
        JOIN projects p ON m.project_id = p.id
        WHERE m.id = %s
        """,
        (mold_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if row["status"] != "drafted":
        raise HTTPException(status_code=409, detail="只有 drafted 项目可以删除模具")
    if row["part_count"] > 0:
        raise HTTPException(status_code=409, detail=f"模具下还有 {row['part_count']} 个零件，请先删除零件")
    db.execute("DELETE FROM molds WHERE id = %s", (mold_id,))
    return {"deleted": True}


@router.delete("/projects/{project_id}/parts/{part_id}")
async def delete_part(
    project_id: int,
    part_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] != "drafted":
        raise HTTPException(status_code=409, detail="Project is not editable")

    db.execute(
        "DELETE FROM project_parts WHERE id = %s AND project_id = %s",
        (part_id, project_id),
    )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 工序 — Processes (project_processes 表)
# ---------------------------------------------------------------------------

def _build_process_code(seq_no: int, method_name: str, faces: str | None) -> str:
    """生成工序编号：P<seq>-<method>[-<faces>]。"""
    parts = [f"P{seq_no}", method_name]
    if faces:
        # 规范化：去空格、大写、用逗号连接
        norm_faces = ",".join(f.strip().upper() for f in faces.split(",") if f.strip())
        if norm_faces:
            parts.append(norm_faces)
    return "-".join(parts)


@router.get("/process-methods")
async def list_process_methods(user: CurrentUser = Depends(_require_internal)):
    """加工方式字典（供前端下拉用）。"""
    rows = db.fetch_all(
        """
        SELECT id, name, category, is_internal_capable, remark
        FROM process_methods
        ORDER BY category, name
        """
    )
    return {"items": rows, "total": len(rows)}


@router.get("/projects/{project_id}/parts/{part_id}/processes")
async def list_processes(
    project_id: int, part_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    # 校验归属
    p = db.fetch_one(
        "SELECT id FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = db.fetch_all(
        """
        SELECT pp.id, pp.seq_no, pp.process_code, pp.method_name,
               pp.faces, pp.notes, pp.est_hours, pm.is_internal_capable,
               pm.category
        FROM project_processes pp
        LEFT JOIN process_methods pm ON pm.name = pp.method_name
        WHERE pp.part_id = %s AND pp.project_id = %s
        ORDER BY pp.seq_no ASC
        """,
        (part_id, project_id),
    )
    for r in rows:
        if r.get("est_hours") is not None:
            r["est_hours"] = float(r["est_hours"])
    return {"items": rows, "total": len(rows)}


@router.post("/projects/{project_id}/parts/{part_id}/processes", status_code=201)
async def add_process(
    project_id: int, part_id: int,
    payload: ProcessCreate,
    user: CurrentUser = Depends(_require_internal),
):
    """新增一道工序；seq_no 省略则自动追加。"""
    p = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if p["status"] != "drafted":
        raise HTTPException(status_code=409, detail="只有 drafted 项目可以编辑工序")

    part = db.fetch_one(
        "SELECT id FROM project_parts WHERE id = %s AND project_id = %s",
        (part_id, project_id),
    )
    if part is None:
        raise HTTPException(status_code=404, detail="Part not found")

    method = db.fetch_one(
        "SELECT name FROM process_methods WHERE name = %s",
        (payload.method_name,),
    )
    if method is None:
        raise HTTPException(status_code=400,
                            detail=f"未知加工方式：{payload.method_name}。请先到 /api/internal/process-methods 查看可选值")

    # 分配 seq_no
    if payload.seq_no is not None:
        seq_no = payload.seq_no
    else:
        row = db.fetch_one(
            "SELECT COALESCE(MAX(seq_no), 0) AS mx FROM project_processes WHERE part_id = %s",
            (part_id,),
        )
        seq_no = (row["mx"] or 0) + 1

    process_code = _build_process_code(seq_no, payload.method_name, payload.faces)

    try:
        new_id = db.execute(
            """
            INSERT INTO project_processes
                (project_id, part_id, seq_no, process_code,
                 method_name, faces, notes, est_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, part_id, seq_no, process_code,
             payload.method_name,
             payload.faces if payload.faces else None,
             payload.notes, payload.est_hours),
        )
    except Exception as e:
        # 唯一约束撞车（seq_no 或 process_code 已存在）
        if "uk_proc_part_seq" in str(e) or "uk_proc_part_code" in str(e):
            raise HTTPException(status_code=409,
                                detail=f"工序号冲突：{process_code} 已存在于该零件下")
        raise

    return {
        "id": new_id,
        "seq_no": seq_no,
        "process_code": process_code,
        "method_name": payload.method_name,
        "faces": payload.faces,
    }


@router.delete("/processes/{process_id}")
async def delete_process(
    process_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    # 校验归属（通过 project 查）
    row = db.fetch_one(
        """
        SELECT pp.id, p.status, p.tenant_id
        FROM project_processes pp
        JOIN projects p ON pp.project_id = p.id
        WHERE pp.id = %s
        """,
        (process_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Process not found")
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if row["status"] != "drafted":
        raise HTTPException(status_code=409, detail="只有 drafted 项目可以编辑工序")

    db.execute("DELETE FROM project_processes WHERE id = %s", (process_id,))
    return {"deleted": True}


@router.get("/processes/{process_id}/faces")
async def list_or_upsert_faces(
    process_id: int,
    labels: Optional[str] = None,
    user: CurrentUser = Depends(_require_internal),
):
    """返回工序下面的面列表；若传了 labels=A,B，则自动插入缺失项后返回 ids。"""
    # 校验归属
    row = db.fetch_one(
        """
        SELECT pp.id, p.tenant_id
        FROM project_processes pp
        JOIN projects p ON pp.project_id = p.id
        WHERE pp.id = %s
        """,
        (process_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)

    # 若传 labels，则先 upsert
    if labels:
        label_list = [s.strip().upper() for s in labels.split(",") if s.strip()]
        # 校验
        for lb in label_list:
            if lb not in "ABCDEF":
                raise HTTPException(status_code=400, detail=f"非法面标签：{lb}")
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                for lb in label_list:
                    cur.execute(
                        """
                        INSERT INTO process_faces (process_id, face_label)
                        VALUES (%s, %s)
                        ON CONFLICT (process_id, face_label) DO NOTHING
                        """,
                        (process_id, lb),
                    )

    # 返回
    rows = db.fetch_all(
        "SELECT id, face_label FROM process_faces WHERE process_id = %s ORDER BY face_label",
        (process_id,),
    )
    if labels:
        wanted = set(s.strip().upper() for s in labels.split(","))
        matched = [r["id"] for r in rows if r["face_label"] in wanted]
        return {"items": rows, "ids": matched}
    return {"items": rows}


@router.post("/projects/{project_id}/attachments", status_code=201)
async def upload_attachment(
    project_id: int,
    file: UploadFile = File(...),
    category: str = Form("drawing"),
    user: CurrentUser = Depends(_require_internal),
):
    project = db.fetch_one(
        "SELECT id FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Save file with unique name
    ext = Path(file.filename or "").suffix
    stored_name = f"{project_id}_{uuid.uuid4().hex}{ext}"
    target_dir = DRAWINGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / stored_name

    with target_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    size = target_path.stat().st_size

    new_id = db.execute(
        """
        INSERT INTO attachments (related_type, related_id, file_name, file_path,
                                 file_size, mime_type, uploaded_by, category)
        VALUES ('project', %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (project_id, file.filename, f"drawings/{stored_name}",
         size, file.content_type, user.user_id, category),
    )
    return {
        "id": new_id,
        "file_name": file.filename,
        "file_size": size,
    }


@router.post("/projects/{project_id}/confirm")
async def confirm_project(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """项目确认 — 检查前置条件后把 status 从 drafted 置为 confirmed。
    前置：
      - 至少 1 个零件
      - 至少 1 份图纸
      - unit_price 和 quantity 已填
    """
    project = db.fetch_one(
        """
        SELECT id, status, unit_price, quantity
        FROM projects WHERE id = %s AND tenant_id = %s
        """,
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] != "drafted":
        raise HTTPException(status_code=409,
                            detail=f"Project already {project['status']}")

    # Check parts
    parts_cnt = db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM project_parts WHERE project_id = %s",
        (project_id,),
    )
    if not parts_cnt or parts_cnt["cnt"] < 1:
        raise HTTPException(status_code=400,
                            detail="至少需要 1 个零件才能确认项目")

    # Check drawings
    draw_cnt = db.fetch_one(
        """
        SELECT COUNT(*) AS cnt FROM attachments
        WHERE related_type = 'project' AND related_id = %s AND category = 'drawing'
        """,
        (project_id,),
    )
    if not draw_cnt or draw_cnt["cnt"] < 1:
        raise HTTPException(status_code=400,
                            detail="至少需要上传 1 份图纸才能确认项目")

    # Check price / quantity
    if not project["unit_price"] or not project["quantity"]:
        raise HTTPException(status_code=400,
                            detail="请先填写报价单价和订单数量")

    db.execute(
        """
        UPDATE projects
        SET status = 'confirmed', confirmed_at = %s
        WHERE id = %s AND tenant_id = %s
        """,
        (datetime.utcnow(), project_id, user.tenant_id),
    )
    return {"status": "confirmed"}


# ---------------------------------------------------------------------------
# File serving (drawings) - 仅受保护的接口
# ---------------------------------------------------------------------------

from fastapi.responses import FileResponse


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    att = db.fetch_one(
        "SELECT file_name, file_path, mime_type FROM attachments WHERE id = %s",
        (attachment_id,),
    )
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    full_path = UPLOADS_DIR / att["file_path"]
    if not full_path.exists():
        raise HTTPException(status_code=410, detail="File missing from storage")

    return FileResponse(
        path=str(full_path),
        filename=att["file_name"],
        media_type=att.get("mime_type") or "application/octet-stream",
    )


# =============================================================================
# Stage 1: Production Decisions + Approvals
# =============================================================================

from mvp.rules import suggest_decision


class DecisionUpdate(BaseModel):
    final_decision: str = Field(..., pattern="^(self_made|outsource)$")
    final_reason: Optional[str] = None


class ApprovalAction(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# POST /api/internal/projects/{id}/decide — trigger hardcoded AI decisions
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/decide")
async def trigger_decide(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """为项目的每个零件×工序生成一行决策建议。
    前置：项目 status = confirmed 或 deciding（重新触发）。
    """
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] not in ("confirmed", "deciding"):
        raise HTTPException(status_code=409,
                            detail=f"Project status must be 'confirmed', now '{project['status']}'")

    # 读所有零件（优先从新表 project_processes 取；若为空回退到老字段 processes_json）
    parts = db.fetch_all(
        "SELECT id, part_no, processes_json FROM project_parts WHERE project_id = %s",
        (project_id,),
    )
    if not parts:
        raise HTTPException(status_code=400, detail="Project has no parts")

    # 检查是否已经存在未定的决策行（重新触发时先清理 pending_review 的）
    db.execute(
        """
        DELETE FROM production_decisions
        WHERE project_id = %s AND status IN ('pending_review', 'reviewed')
        """,
        (project_id,),
    )

    created = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for pt in parts:
                # 1) 优先从 project_processes 取结构化工序
                cur.execute(
                    """
                    SELECT id, seq_no, process_code, method_name, faces
                    FROM project_processes
                    WHERE part_id = %s
                    ORDER BY seq_no ASC
                    """,
                    (pt["id"],),
                )
                structured = cur.fetchall()

                if structured:
                    for row in structured:
                        # row 在默认 cursor 下是 tuple
                        pid, seq, code, method_name, faces = row
                        sug = suggest_decision(method_name)
                        # faces 字符串 "A,B" → face_ids[] 需要查 process_faces（MVP 只存层级，不强制）
                        # 为简化，face_ids 暂时传 NULL（scope_level=method 即可，不下沉到面）
                        cur.execute(
                            """
                            INSERT INTO production_decisions
                                (project_id, part_id, process_id, process_name,
                                 scope_level,
                                 ai_suggestion, ai_reason, ai_source, is_forced,
                                 final_decision, status)
                            VALUES (%s, %s, %s, %s, 'method',
                                    %s, %s, 'rules', %s, %s, 'pending_review')
                            """,
                            (project_id, pt["id"], pid, method_name,
                             sug.decision, sug.reason, sug.is_forced,
                             sug.decision),
                        )
                        created += 1
                else:
                    # 2) 回退：老的逗号字符串 / JSON 列表字段
                    procs_raw = pt.get("processes_json")
                    procs = (
                        json.loads(procs_raw) if isinstance(procs_raw, str) and procs_raw
                        else (procs_raw or [])
                    )
                    for proc in procs:
                        sug = suggest_decision(proc)
                        cur.execute(
                            """
                            INSERT INTO production_decisions
                                (project_id, part_id, process_name,
                                 scope_level,
                                 ai_suggestion, ai_reason, ai_source, is_forced,
                                 final_decision, status)
                            VALUES (%s, %s, %s, 'method',
                                    %s, %s, 'rules', %s, %s, 'pending_review')
                            """,
                            (project_id, pt["id"], proc,
                             sug.decision, sug.reason, sug.is_forced,
                             sug.decision),
                        )
                        created += 1

            # 项目进入 deciding 状态
            cur.execute(
                "UPDATE projects SET status = 'deciding' WHERE id = %s",
                (project_id,),
            )

    return {"created": created, "status": "deciding"}


@router.get("/projects/{project_id}/decisions")
async def list_decisions(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """Return all production_decisions for a project."""
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = db.fetch_all(
        """
        SELECT d.id, d.part_id, d.process_name,
               d.ai_suggestion, d.ai_reason, d.ai_source, d.is_forced,
               d.final_decision, d.final_reason,
               d.reviewed_by, d.reviewed_at, d.status,
               d.approval_task_id, d.created_at,
               p.part_no, p.part_name, p.material
        FROM production_decisions d
        JOIN project_parts p ON d.part_id = p.id
        WHERE d.project_id = %s
        ORDER BY p.id ASC, d.id ASC
        """,
        (project_id,),
    )
    # 转 is_forced 为 bool
    for r in rows:
        r["is_forced"] = bool(r.get("is_forced"))
    return {
        "project_status": project["status"],
        "items": rows,
        "total": len(rows),
    }


@router.patch("/decisions/{decision_id}")
async def update_decision(
    decision_id: int,
    payload: DecisionUpdate,
    user: CurrentUser = Depends(_require_internal),
):
    """人工修改单行决策。强制委外行只能保持 outsource。"""
    row = db.fetch_one(
        """
        SELECT d.id, d.is_forced, d.ai_suggestion, d.status, p.tenant_id
        FROM production_decisions d
        JOIN projects p ON d.project_id = p.id
        WHERE d.id = %s
        """,
        (decision_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not your project")
    if row["status"] not in ("pending_review", "reviewed"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot edit decision in status '{row['status']}'")

    if row["is_forced"] and payload.final_decision == "self_made":
        raise HTTPException(status_code=409,
                            detail="该工序为强制委外项，不允许改为自制")

    db.execute(
        """
        UPDATE production_decisions
        SET final_decision = %s,
            final_reason   = %s,
            reviewed_by    = %s,
            reviewed_at    = %s,
            status         = 'reviewed'
        WHERE id = %s
        """,
        (payload.final_decision, payload.final_reason, user.user_id,
         datetime.utcnow(), decision_id),
    )
    return {"updated": True}


@router.post("/projects/{project_id}/decisions/submit")
async def submit_decisions(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """提交批次审批 → 创建 1 条 workflow_approval_tasks 给生产经理。"""
    project = db.fetch_one(
        "SELECT id, status FROM projects WHERE id = %s AND tenant_id = %s",
        (project_id, user.tenant_id),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["status"] != "deciding":
        raise HTTPException(status_code=409,
                            detail=f"Project not in 'deciding' state: {project['status']}")

    # 确保所有决策行都已 reviewed（final_decision 非空即可）
    missing = db.fetch_one(
        """
        SELECT COUNT(*) AS cnt FROM production_decisions
        WHERE project_id = %s AND (final_decision IS NULL OR final_decision = '')
        """,
        (project_id,),
    )
    if missing and missing["cnt"] > 0:
        raise HTTPException(status_code=400,
                            detail=f"还有 {missing['cnt']} 行决策未审核")

    # Check existing approval task; avoid double-submit
    existing = db.fetch_one(
        """
        SELECT id FROM workflow_approval_tasks
        WHERE assignee_type = 'role' AND assignee_id = 'ADMIN'
          AND node_id = %s AND status = 'pending'
        """,
        (f"production_decision:{project_id}",),
    )
    if existing:
        return {"submitted": True, "task_id": existing["id"], "note": "已有待办，未重复创建"}

    # 创建审批任务 + 初始 SUBMIT 记录 + 状态事件 (单事务)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1) Task
            # workflow_approval_tasks.instance_id 在 PG 里是 UUID；MVP 不强绑定工作流实例，
            # 用 uuid5(project_id) 产生一个稳定的占位 UUID。
            import uuid as _uuid
            fake_instance_id = _uuid.uuid5(_uuid.NAMESPACE_URL, f"project:{project_id}")

            cur.execute(
                """
                INSERT INTO workflow_approval_tasks
                    (instance_id, node_id, assignee_type, assignee_id, status)
                VALUES (%s, %s, 'role', 'ADMIN', 'pending')
                RETURNING id
                """,
                (str(fake_instance_id), f"production_decision:{project_id}"),
            )
            task_id = cur.fetchone()[0]

            # 2) Initial SUBMIT record
            record_uuid = _uuid.uuid4()
            cur.execute(
                """
                INSERT INTO workflow_approval_records
                    (id, instance_id, node_id, action, actor_id,
                     assignee_type, assignee_id, comment, metadata_json, created_at)
                VALUES (%s, %s, %s, 'submit', %s, 'role', 'ADMIN', %s, %s, %s)
                """,
                (str(record_uuid), str(fake_instance_id),
                 f"production_decision:{project_id}",
                 str(user.user_id),
                 f"提交生产决策: project_id={project_id}",
                 json.dumps({
                     "kind": "production_decision",
                     "project_id": project_id,
                     "task_id": task_id,
                 }, ensure_ascii=False),
                 datetime.utcnow()),
            )

            # 3) State event
            cur.execute(
                """
                INSERT INTO workflow_state_events
                    (instance_id, node_id, event_type, to_status, payload_json, occurred_at)
                VALUES (%s, %s, 'approval_task_created', 'pending', %s, %s)
                """,
                (str(fake_instance_id),
                 f"production_decision:{project_id}",
                 json.dumps({"project_id": project_id, "task_id": task_id}, ensure_ascii=False),
                 datetime.utcnow()),
            )

            # 4) 把决策行 status → submitted，并关联 task_id
            cur.execute(
                """
                UPDATE production_decisions
                SET status = 'submitted', approval_task_id = %s
                WHERE project_id = %s
                """,
                (task_id, project_id),
            )

    return {"submitted": True, "task_id": task_id}


# ---------------------------------------------------------------------------
# Approval — 待办 + 查看 + 执行
# ---------------------------------------------------------------------------

@router.get("/approvals/pending")
async def list_pending_approvals(
    user: CurrentUser = Depends(_require_internal),
):
    """当前用户能看到的待办。

    方案 B：internal 租户的任意用户都能看到 role='ADMIN' 或 assignee=自己 的待办。
    """
    role_match = (user.role or "").upper()

    rows = db.fetch_all(
        """
        SELECT t.id, t.instance_id::text AS instance_hex, t.node_id,
               t.assignee_type, t.assignee_id, t.status, t.due_at, t.created_at
        FROM workflow_approval_tasks t
        WHERE t.status = 'pending'
          AND (
               (t.assignee_type = 'user' AND t.assignee_id = %s)
            OR (t.assignee_type = 'role' AND t.assignee_id IN ('ADMIN', %s))
          )
        ORDER BY t.created_at DESC
        LIMIT 100
        """,
        (str(user.user_id), role_match),
    )

    # For each task, 附加业务摘要（从 node_id 提取 project_id 并取名称）
    for r in rows:
        node = r.get("node_id") or ""
        r["kind"] = node.split(":", 1)[0] if ":" in node else node
        r["subject_id"] = node.split(":", 1)[1] if ":" in node else None
        r["due_at"] = r["due_at"].isoformat() if r["due_at"] else None
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None

        # Try to enrich with project name
        if r["kind"] == "production_decision" and r["subject_id"]:
            try:
                p = db.fetch_one(
                    "SELECT name, project_no FROM projects WHERE id = %s",
                    (int(r["subject_id"]),),
                )
                if p:
                    r["subject_title"] = f"{p['project_no']} — {p['name']}"
            except Exception:
                r["subject_title"] = None

    return {"items": rows, "total": len(rows)}


@router.get("/approvals/{task_id}")
async def get_approval_detail(
    task_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """审批任务详情 + 业务载荷。"""
    task = db.fetch_one(
        """
        SELECT id, instance_id::text AS instance_hex, node_id,
               assignee_type, assignee_id, status, due_at, created_at
        FROM workflow_approval_tasks WHERE id = %s
        """,
        (task_id,),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    # 权限：任务 assignee 必须匹配当前用户
    role_match = (user.role or "").upper()
    is_mine = (
        (task["assignee_type"] == "user" and task["assignee_id"] == str(user.user_id)) or
        (task["assignee_type"] == "role" and task["assignee_id"] in ("ADMIN", role_match))
    )
    if not is_mine:
        raise HTTPException(status_code=403, detail="Not your task")

    node = task["node_id"] or ""
    kind = node.split(":", 1)[0] if ":" in node else node
    subject_id = node.split(":", 1)[1] if ":" in node else None
    task["kind"] = kind
    task["subject_id"] = subject_id
    task["created_at"] = task["created_at"].isoformat() if task["created_at"] else None

    # Load payload based on kind
    payload: dict = {}
    if kind == "production_decision" and subject_id:
        project_id = int(subject_id)
        project = db.fetch_one(
            """
            SELECT id, project_no, name, customer, deadline, unit_price, quantity
            FROM projects WHERE id = %s
            """,
            (project_id,),
        )
        decisions = db.fetch_all(
            """
            SELECT d.id, d.process_name, d.ai_suggestion, d.ai_reason,
                   d.is_forced, d.final_decision, d.final_reason,
                   p.part_no, p.part_name, p.material
            FROM production_decisions d
            JOIN project_parts p ON d.part_id = p.id
            WHERE d.project_id = %s
            ORDER BY p.id ASC, d.id ASC
            """,
            (project_id,),
        )
        for d in decisions:
            d["is_forced"] = bool(d.get("is_forced"))
        payload = {"project": project, "decisions": decisions}

    elif kind == "outsource_award" and subject_id:
        # 采购经理审批：打包所有已报价邀请为表格
        request_id = int(subject_id)
        req = db.fetch_one(
            """
            SELECT r.*, p.project_no, p.name AS project_name, p.customer,
                   p.unit_price AS client_unit_price
            FROM outsource_requests r
            JOIN projects p ON r.project_id = p.id
            WHERE r.id = %s
            """,
            (request_id,),
        )
        if req:
            raw = req.get("required_processes_json")
            req["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
            req.pop("required_processes_json", None)

        invs = db.fetch_all(
            """
            SELECT i.id AS invitation_id, i.supplier_id, i.invitation_status,
                   s.name AS supplier_name, s.category AS supplier_category,
                   q.id AS quotation_id, q.unit_price, q.lead_time_days,
                   q.note, q.submitted_at
            FROM outsource_request_invitations i
            JOIN suppliers s ON i.supplier_id = s.id
            LEFT JOIN outsource_quotations q ON q.invitation_id = i.id
            WHERE i.request_id = %s AND i.invitation_status = 'quoted'
            ORDER BY q.unit_price ASC
            """,
            (request_id,),
        )
        for v in invs:
            if v.get("unit_price") is not None:
                v["unit_price"] = float(v["unit_price"])
            if v.get("submitted_at"):
                v["submitted_at"] = v["submitted_at"].isoformat()

        # 新：5 层 scope_items + quotation_lines 矩阵
        scope_items = db.fetch_all(
            """
            SELECT * FROM v_outsource_scope_display
            WHERE request_id = %s
            ORDER BY scope_item_id
            """,
            (request_id,),
        )

        quotation_lines = db.fetch_all(
            """
            SELECT ql.id AS line_id, ql.quotation_id, ql.scope_item_id,
                   ql.unit_price, ql.lead_time_days, ql.note,
                   i.id AS invitation_id, i.supplier_id,
                   s.name AS supplier_name
            FROM outsource_quotation_lines ql
            JOIN outsource_quotations q ON ql.quotation_id = q.id
            JOIN outsource_request_invitations i ON q.invitation_id = i.id
            JOIN suppliers s ON i.supplier_id = s.id
            WHERE i.request_id = %s AND i.invitation_status = 'quoted'
            ORDER BY ql.scope_item_id, ql.unit_price ASC
            """,
            (request_id,),
        )
        for l in quotation_lines:
            l["unit_price"] = float(l["unit_price"])

        payload = {
            "request": req,
            "quotations": invs,
            "scope_items": scope_items,
            "quotation_lines": quotation_lines,
        }

    # Approval history
    history = db.fetch_all(
        """
        SELECT id::text AS record_id, action, actor_id, comment, created_at
        FROM workflow_approval_records
        WHERE instance_id = %s::uuid
        ORDER BY created_at ASC
        """,
        (task["instance_hex"],),
    )
    for h in history:
        h["created_at"] = h["created_at"].isoformat() if h["created_at"] else None

    return {
        "task": task,
        "payload": payload,
        "history": history,
    }


@router.post("/approvals/{task_id}/action")
async def act_approval(
    task_id: int,
    payload: ApprovalAction,
    user: CurrentUser = Depends(_require_internal),
):
    """APPROVE/REJECT 审批任务。"""
    task = db.fetch_one(
        """
        SELECT id, instance_id::text AS instance_hex, node_id,
               assignee_type, assignee_id, status
        FROM workflow_approval_tasks WHERE id = %s
        """,
        (task_id,),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"Task already {task['status']}")

    # 权限
    role_match = (user.role or "").upper()
    is_mine = (
        (task["assignee_type"] == "user" and task["assignee_id"] == str(user.user_id)) or
        (task["assignee_type"] == "role" and task["assignee_id"] in ("ADMIN", role_match))
    )
    if not is_mine:
        raise HTTPException(status_code=403, detail="Not your task")

    node = task["node_id"] or ""
    kind = node.split(":", 1)[0] if ":" in node else node
    subject_id = node.split(":", 1)[1] if ":" in node else None

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1) Append record
            import uuid as _uuid
            record_uuid = _uuid.uuid4()
            action_value = "approve" if payload.action == "approve" else "reject"
            cur.execute(
                """
                INSERT INTO workflow_approval_records
                    (id, instance_id, node_id, action, actor_id,
                     assignee_type, assignee_id, comment, created_at)
                VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
                """,
                (str(record_uuid), task["instance_hex"], node, action_value,
                 str(user.user_id), task["assignee_type"], task["assignee_id"],
                 payload.comment or "", datetime.utcnow()),
            )

            # 2) Task → completed
            cur.execute(
                """
                UPDATE workflow_approval_tasks
                SET status = 'completed',
                    claimed_by = %s,
                    completed_at = %s,
                    completion_action = %s
                WHERE id = %s
                """,
                (str(user.user_id), datetime.utcnow(), action_value, task_id),
            )

            # 3) State event
            cur.execute(
                """
                INSERT INTO workflow_state_events
                    (instance_id, node_id, event_type, from_status, to_status,
                     payload_json, occurred_at)
                VALUES (%s::uuid, %s, 'approval_action_taken', 'pending', %s, %s, %s)
                """,
                (task["instance_hex"], node,
                 "approved" if action_value == "approve" else "rejected",
                 json.dumps({
                     "actor_id": user.user_id,
                     "action": action_value,
                     "comment": payload.comment,
                 }, ensure_ascii=False),
                 datetime.utcnow()),
            )

            # 4) 业务后处理：kind = production_decision
            outsource_request_id = None
            if kind == "production_decision" and subject_id:
                project_id = int(subject_id)
                if action_value == "approve":
                    # 决策行全部 finalized
                    cur.execute(
                        """
                        UPDATE production_decisions
                        SET status = 'finalized'
                        WHERE project_id = %s
                        """,
                        (project_id,),
                    )
                    # 项目 → decided
                    cur.execute(
                        "UPDATE projects SET status = 'decided' WHERE id = %s",
                        (project_id,),
                    )

                    # 聚合 outsource 行，生成 1 张询价单草稿（阶段 3 的表可能还未建，做兼容处理）
                    cur.execute("SELECT to_regclass('public.outsource_requests') AS t")
                    reg = cur.fetchone()
                    reg_exists = reg[0] if reg else None
                    if reg_exists:
                        cur.execute(
                            """
                            SELECT COUNT(*) AS cnt
                            FROM production_decisions
                            WHERE project_id = %s AND final_decision = 'outsource'
                            """,
                            (project_id,),
                        )
                        row = cur.fetchone()
                        n_out = row["cnt"] if isinstance(row, dict) else row[0]

                        if n_out > 0:
                            # 简化：一张询价单
                            cur.execute(
                                """
                                SELECT STRING_AGG(DISTINCT d.process_name, ',' ORDER BY d.process_name) AS procs,
                                       MAX(p.quantity) AS qty,
                                       MAX(p.deadline) AS due
                                FROM production_decisions d
                                JOIN projects p ON d.project_id = p.id
                                WHERE d.project_id = %s AND d.final_decision = 'outsource'
                                """,
                                (project_id,),
                            )
                            row = cur.fetchone()
                            procs_csv = row["procs"] if isinstance(row, dict) else row[0]
                            procs = [s for s in (procs_csv or "").split(",") if s]
                            qty = (row["qty"] if isinstance(row, dict) else row[1]) or 1
                            due = (row["due"] if isinstance(row, dict) else row[2])

                            proj = db.fetch_one(
                                "SELECT project_no, name FROM projects WHERE id = %s",
                                (project_id,),
                            )
                            title = f"{proj['project_no']} - {proj['name']}" if proj else f"项目 {project_id}"

                            cur.execute(
                                """
                                INSERT INTO outsource_requests
                                    (project_id, title, required_processes_json,
                                     quantity, deadline, status)
                                VALUES (%s, %s, %s, %s, %s, 'draft')
                                RETURNING id
                                """,
                                (project_id, title,
                                 json.dumps(procs, ensure_ascii=False),
                                 qty, due),
                            )
                            outsource_request_id = cur.fetchone()[0]

                            # ===== 按决策行自动创建 scope_items =====
                            # 读出所有 outsource 决策行（含 scope 信息）
                            cur.execute(
                                """
                                SELECT d.id, d.part_id, d.process_id, d.process_name,
                                       d.scope_level, d.face_ids,
                                       pp.mold_id, pp.qty AS part_qty
                                FROM production_decisions d
                                JOIN project_parts pp ON d.part_id = pp.id
                                WHERE d.project_id = %s AND d.final_decision = 'outsource'
                                ORDER BY d.id
                                """,
                                (project_id,),
                            )
                            dec_rows = cur.fetchall()
                            for idx, drow in enumerate(dec_rows, start=1):
                                d_id, part_id, process_id, proc_name, \
                                    scope_level, face_ids, mold_id, part_qty = drow
                                # 根据实际数据确定合法的 scope_level，
                                # 满足 ck_scope_required 约束
                                level = scope_level or ('face' if face_ids else 'method')
                                # 降级逻辑：数据不满足高级别约束时自动降级
                                if level == 'face' and (not face_ids):
                                    level = 'method'
                                if level == 'method' and (process_id is None):
                                    level = 'part'
                                if level == 'part' and (part_id is None):
                                    level = 'mold' if mold_id else 'project'
                                if level == 'mold' and (mold_id is None):
                                    level = 'project'
                                desc = f"决策行 #{d_id}: {proc_name}" + (
                                    f" (面: {face_ids})" if face_ids else ""
                                )
                                cur.execute(
                                    """
                                    INSERT INTO outsource_scope_items
                                        (request_id, scope_level, project_id, mold_id,
                                         part_id, process_id, face_ids,
                                         quantity, description, sort_no, status)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                                    """,
                                    (outsource_request_id, level, project_id, mold_id,
                                     part_id, process_id, face_ids,
                                     part_qty or 1, desc, idx),
                                )

                else:  # reject
                    # 决策行退回 pending_review
                    cur.execute(
                        """
                        UPDATE production_decisions
                        SET status = 'pending_review', approval_task_id = NULL
                        WHERE project_id = %s
                        """,
                        (project_id,),
                    )
                    # 项目回 deciding
                    cur.execute(
                        "UPDATE projects SET status = 'deciding' WHERE id = %s",
                        (project_id,),
                    )

    return {
        "status": "ok",
        "action": action_value,
        "outsource_request_id": outsource_request_id,
    }


# =============================================================================
# Stage 3: Outsource Requests (我方端)
# =============================================================================


class OutsourceRequestUpdate(BaseModel):
    title: Optional[str] = None
    deadline: Optional[str] = None
    quantity: Optional[int] = None
    material_sourcing: Optional[str] = Field(None, pattern="^(internal|processor)$")


class AwardAction(BaseModel):
    """采购经理审批时的特殊载荷。
    两种中标模式：
      - 传统整单：approve + awarded_invitation_id（兼容老前端，不传 awarded_lines）
      - 逐项中标：approve + awarded_lines=[quotation_line_id, ...]（新前端），
                  每个 line 生成一张 outsource_order
    """
    action: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = None
    awarded_invitation_id: Optional[int] = None   # 传统整单中标
    awarded_lines: Optional[list[int]] = None     # 新：逐项中标（quotation_line_id 数组）


def _next_order_no() -> str:
    """Generate outsource_orders.order_no."""
    return f"OO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------------
# 询价单列表 / 详情
# ---------------------------------------------------------------------------

@router.get("/outsource-requests")
async def list_outsource_requests(
    user: CurrentUser = Depends(_require_internal),
    status_filter: Optional[str] = None,
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if status_filter:
        where += " AND r.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT r.id, r.project_id, r.title, r.required_processes_json,
               r.quantity, r.deadline, r.status,
               r.approval_task_id, r.winning_quotation_id,
               r.created_at, r.updated_at,
               p.project_no, p.name AS project_name
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        {where}
        ORDER BY r.created_at DESC
        LIMIT 50
        """,
        tuple(params),
    )
    for r in rows:
        raw = r.get("required_processes_json")
        r["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
        r.pop("required_processes_json", None)
    return {"items": rows, "total": len(rows)}


@router.get("/outsource-requests/{req_id}")
async def get_outsource_request(
    req_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    req = db.fetch_one(
        """
        SELECT r.*, p.project_no, p.name AS project_name, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if req is None:
        raise HTTPException(status_code=404, detail="Outsource request not found")
    if req["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not your project")

    raw = req.get("required_processes_json")
    req["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    req.pop("required_processes_json", None)

    # Invitations + quotations
    invs = db.fetch_all(
        """
        SELECT i.id, i.supplier_id, i.invitation_status,
               i.sent_at, i.quoted_at,
               s.name AS supplier_name, s.category AS supplier_category,
               q.id AS quotation_id, q.unit_price, q.lead_time_days, q.note,
               q.submitted_at
        FROM outsource_request_invitations i
        JOIN suppliers s ON i.supplier_id = s.id
        LEFT JOIN outsource_quotations q ON q.invitation_id = i.id
        WHERE i.request_id = %s
        ORDER BY i.id
        """,
        (req_id,),
    )
    for v in invs:
        if v.get("unit_price") is not None:
            v["unit_price"] = float(v["unit_price"])
        if v.get("sent_at"):
            v["sent_at"] = v["sent_at"].isoformat()
        if v.get("quoted_at"):
            v["quoted_at"] = v["quoted_at"].isoformat()
        if v.get("submitted_at"):
            v["submitted_at"] = v["submitted_at"].isoformat()

    # Stats
    total_inv = len(invs)
    quoted = sum(1 for v in invs if v["invitation_status"] == "quoted")

    return {
        "request": req,
        "invitations": invs,
        "stats": {
            "total_invited": total_inv,
            "quoted": quoted,
            "no_response": sum(1 for v in invs if v["invitation_status"] == "no_response"),
            "pending": total_inv - quoted - sum(1 for v in invs if v["invitation_status"] == "no_response"),
        },
    }


@router.patch("/outsource-requests/{req_id}")
async def update_outsource_request(
    req_id: int,
    payload: OutsourceRequestUpdate,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        """
        SELECT r.id, r.status, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if row["status"] != "draft":
        raise HTTPException(status_code=409,
                            detail=f"Only draft requests can be edited; now '{row['status']}'")

    updates: list[str] = []
    params: list[Any] = []
    for f, v in payload.model_dump(exclude_unset=True).items():
        updates.append(f"{f} = %s")
        params.append(v)
    if not updates:
        return {"updated": False}
    params.append(req_id)
    db.execute(
        f"UPDATE outsource_requests SET {', '.join(updates)} WHERE id = %s",
        tuple(params),
    )
    return {"updated": True}


# ---------------------------------------------------------------------------
# 委外项 Scope Items — 支持 5 层粒度（project/mold/part/method/face）
# ---------------------------------------------------------------------------

class ScopeItemCreate(BaseModel):
    scope_level: str                   # project / mold / part / method / face
    mold_id:    Optional[int] = None
    part_id:    Optional[int] = None
    process_id: Optional[int] = None
    face_ids:   Optional[list[int]] = None
    quantity:   int = 1
    description: Optional[str] = None


@router.get("/outsource-requests/{req_id}/scope-items")
async def list_scope_items(req_id: int, user: CurrentUser = Depends(_require_internal)):
    # 归属校验
    req = db.fetch_one(
        """
        SELECT r.id, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if req is None or req["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    rows = db.fetch_all(
        """
        SELECT v.*, s.sort_no
        FROM v_outsource_scope_display v
        JOIN outsource_scope_items s ON s.id = v.scope_item_id
        WHERE v.request_id = %s
        ORDER BY s.sort_no NULLS LAST, v.scope_item_id
        """,
        (req_id,),
    )
    return {"items": rows, "total": len(rows)}


@router.post("/outsource-requests/{req_id}/scope-items", status_code=201)
async def add_scope_item(
    req_id: int,
    payload: ScopeItemCreate,
    user: CurrentUser = Depends(_require_internal),
):
    """新增委外项。根据 scope_level 校验必填字段：
      - project : 不需额外
      - mold    : mold_id
      - part    : part_id
      - method  : part_id + process_id
      - face    : part_id + process_id + face_ids
    """
    req = db.fetch_one(
        """
        SELECT r.id, r.status, r.project_id, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if req is None or req["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)
    if req["status"] != "draft":
        raise HTTPException(status_code=409, detail="只有 draft 询价单可以编辑委外项")

    # 校验必填 + 归属
    level = payload.scope_level
    if level not in ("project", "mold", "part", "method", "face"):
        raise HTTPException(status_code=400, detail=f"未知 scope_level: {level}")

    project_id = req["project_id"]
    mold_id    = payload.mold_id
    part_id    = payload.part_id
    process_id = payload.process_id
    face_ids   = payload.face_ids or []

    if level == "mold" and mold_id is None:
        raise HTTPException(status_code=400, detail="mold 级需要 mold_id")
    if level == "part" and part_id is None:
        raise HTTPException(status_code=400, detail="part 级需要 part_id")
    if level == "method" and (part_id is None or process_id is None):
        raise HTTPException(status_code=400, detail="method 级需要 part_id 和 process_id")
    if level == "face" and (part_id is None or process_id is None or not face_ids):
        raise HTTPException(status_code=400, detail="face 级需要 part_id、process_id 和 face_ids")

    # 补齐 mold_id（从 part 取）
    if part_id is not None and mold_id is None:
        part = db.fetch_one(
            "SELECT mold_id FROM project_parts WHERE id=%s AND project_id=%s",
            (part_id, project_id),
        )
        if part:
            mold_id = part["mold_id"]

    # sort_no
    row = db.fetch_one(
        "SELECT COALESCE(MAX(sort_no), 0) AS mx FROM outsource_scope_items WHERE request_id = %s",
        (req_id,),
    )
    sort_no = (row["mx"] or 0) + 1

    try:
        new_id = db.execute(
            """
            INSERT INTO outsource_scope_items
                (request_id, scope_level, project_id, mold_id, part_id,
                 process_id, face_ids, quantity, description, sort_no)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req_id, level, project_id, mold_id, part_id,
             process_id, face_ids if face_ids else None,
             payload.quantity, payload.description, sort_no),
        )
    except Exception as e:
        if "ck_scope_required" in str(e):
            raise HTTPException(status_code=400, detail="scope_level 与字段组合不匹配")
        if "ck_scope_level" in str(e):
            raise HTTPException(status_code=400, detail="非法 scope_level")
        raise
    return {"id": new_id, "sort_no": sort_no}


@router.delete("/scope-items/{item_id}")
async def delete_scope_item(item_id: int, user: CurrentUser = Depends(_require_internal)):
    row = db.fetch_one(
        """
        SELECT s.id, r.status, p.tenant_id
        FROM outsource_scope_items s
        JOIN outsource_requests r ON s.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE s.id = %s
        """,
        (item_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if row["status"] != "draft":
        raise HTTPException(status_code=409, detail="只有 draft 询价单可以删除委外项")
    db.execute("DELETE FROM outsource_scope_items WHERE id = %s", (item_id,))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 候选供应商匹配（固定算法，不用 AI）
# ---------------------------------------------------------------------------

@router.get("/outsource-requests/{req_id}/candidates")
async def list_candidates(
    req_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """返回符合工艺要求的供应商列表，按匹配工艺数降序。"""
    req = db.fetch_one(
        """
        SELECT r.id, r.required_processes_json, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if req is None:
        raise HTTPException(status_code=404)
    if req["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)

    raw = req.get("required_processes_json")
    procs = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    if not procs:
        return {"items": [], "total": 0, "required": []}

    placeholders = ",".join(["%s"] * len(procs))
    rows = db.fetch_all(
        f"""
        SELECT s.id, s.name, s.category, s.contact_name, s.contact_phone,
               COUNT(DISTINCT c.process_name) AS match_count,
               STRING_AGG(DISTINCT c.process_name, ',' ORDER BY c.process_name) AS matched_procs
        FROM suppliers s
        JOIN supplier_capabilities c ON c.supplier_id = s.id
        WHERE c.process_name IN ({placeholders})
        GROUP BY s.id
        ORDER BY match_count DESC, s.name ASC
        """,
        tuple(procs),
    )
    # Enrich: 是否已邀请
    invited = db.fetch_all(
        "SELECT supplier_id FROM outsource_request_invitations WHERE request_id = %s",
        (req_id,),
    )
    invited_ids = {i["supplier_id"] for i in invited}
    for r in rows:
        r["match_count"] = int(r["match_count"])
        r["matched_processes"] = (r["matched_procs"] or "").split(",") if r["matched_procs"] else []
        r.pop("matched_procs", None)
        r["already_invited"] = r["id"] in invited_ids
        r["coverage"] = f"{r['match_count']}/{len(procs)}"

    return {"items": rows, "total": len(rows), "required": procs}


# ---------------------------------------------------------------------------
# 群发询价
# ---------------------------------------------------------------------------

@router.post("/outsource-requests/{req_id}/send")
async def broadcast_invitations(
    req_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """群发给所有符合工艺的候选加工方。
    若 material_sourcing='internal'，同时创建材料询价单并群发给材料方。
    """
    # Reuse candidate logic
    cand = await list_candidates(req_id, user)
    if not cand["items"]:
        raise HTTPException(status_code=400, detail="没有符合工艺要求的候选加工方")

    req = db.fetch_one(
        "SELECT id, status, material_sourcing, project_id FROM outsource_requests WHERE id = %s",
        (req_id,),
    )
    if req["status"] not in ("draft",):
        raise HTTPException(status_code=409,
                            detail=f"只有 draft 状态可以群发；当前 '{req['status']}'")

    # 校验 material_sourcing 已选择
    if not req["material_sourcing"]:
        raise HTTPException(status_code=400, detail="请先选择材料供应方式")

    # 查 tenant 对应 supplier
    tenants_map = {
        r["supplier_id"]: r["id"]
        for r in db.fetch_all(
            "SELECT id, supplier_id FROM tenants WHERE tenant_type = 'processor' AND supplier_id IS NOT NULL",
        )
        if r["supplier_id"]
    }

    now = datetime.utcnow()
    inserted = 0
    material_inquiry_id = None
    material_inquiry_no = None
    invited_material_count = 0

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # --- 加工方邀请（不变） ---
            for c in cand["items"]:
                if c["already_invited"]:
                    continue
                cur.execute(
                    """
                    INSERT INTO outsource_request_invitations
                        (request_id, supplier_id, tenant_id, invitation_status, sent_at)
                    VALUES (%s, %s, %s, 'sent', %s)
                    """,
                    (req_id, c["id"], tenants_map.get(c["id"]), now),
                )
                inserted += 1

            # --- 材料询价（仅 internal 模式） ---
            if req["material_sourcing"] == "internal":
                project_id = req["project_id"]

                # 从 project_parts 提取去重的 (material, spec) + 汇总数量
                cur.execute(
                    """
                    SELECT material, spec, SUM(qty) AS total_qty
                    FROM project_parts
                    WHERE project_id = %s AND material IS NOT NULL AND material != ''
                    GROUP BY material, spec
                    ORDER BY material, spec
                    """,
                    (project_id,),
                )
                bom_rows = cur.fetchall()
                if not bom_rows:
                    raise HTTPException(status_code=400,
                                        detail="项目零件清单中没有材料信息，无法创建材料询价")

                # 生成询价单号
                cur.execute("SELECT project_no FROM projects WHERE id=%s", (project_id,))
                proj_row = cur.fetchone()
                pn = proj_row[0] if proj_row else f"P{project_id}"
                material_inquiry_no = f"MI-{pn}-{uuid.uuid4().hex[:4].upper()}"

                # 创建 material_inquiries
                title = f"{pn} 材料询价"
                cur.execute(
                    """
                    INSERT INTO material_inquiries
                        (inquiry_no, outsource_request_id, outsource_order_id, project_id,
                         title, material_code, status, created_by)
                    VALUES (%s, %s, NULL, %s, %s, %s, 'inviting', %s)
                    RETURNING id
                    """,
                    (material_inquiry_no, req_id, project_id, title,
                     bom_rows[0][0],  # material_code 取第一行（兼容旧字段）
                     user.user_id),
                )
                material_inquiry_id = cur.fetchone()[0]

                # 创建 material_inquiry_lines
                for idx, (mat_code, spec, total_qty) in enumerate(bom_rows, start=1):
                    cur.execute(
                        """
                        INSERT INTO material_inquiry_lines
                            (inquiry_id, material_code, material_name, spec, qty, unit, sort_order)
                        VALUES (%s, %s, NULL, %s, %s, 'kg', %s)
                        """,
                        (material_inquiry_id, mat_code, spec, total_qty, idx),
                    )

                # 群发给所有材料方租户
                cur.execute(
                    """
                    SELECT t.id AS tenant_id, t.supplier_id
                    FROM tenants t
                    WHERE t.tenant_type='material' AND t.supplier_id IS NOT NULL
                    """
                )
                mat_tenants = cur.fetchall()
                for mt in mat_tenants:
                    t_id, s_id = mt
                    cur.execute(
                        """
                        INSERT INTO material_inquiry_invitations
                            (inquiry_id, supplier_id, tenant_id, invitation_status, sent_at)
                        VALUES (%s, %s, %s, 'sent', %s)
                        ON CONFLICT (inquiry_id, supplier_id) DO NOTHING
                        """,
                        (material_inquiry_id, s_id, t_id, now),
                    )
                    if cur.rowcount:
                        invited_material_count += 1

            cur.execute(
                "UPDATE outsource_requests SET status = 'inviting' WHERE id = %s",
                (req_id,),
            )

    result = {
        "invited_processor_count": inserted,
        "total_candidates": len(cand["items"]),
        "status": "inviting",
        "material_sourcing": req["material_sourcing"],
    }
    if req["material_sourcing"] == "internal":
        result["material_inquiry_id"] = material_inquiry_id
        result["material_inquiry_no"] = material_inquiry_no
        result["invited_material_supplier_count"] = invited_material_count
    else:
        result["material_inquiry_id"] = None
        result["invited_material_supplier_count"] = None

    return result


# ---------------------------------------------------------------------------
# 截止报价 → 创建采购经理审批
# ---------------------------------------------------------------------------

@router.post("/outsource-requests/{req_id}/close-quoting")
async def close_quoting(
    req_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """采购员点【截止报价】→ 把所有 sent 未回复的邀请置 no_response；
    打包所有已回复报价 → 创建 1 条审批任务给采购经理 (PURCHASING_MANAGER)。
    """
    req = db.fetch_one(
        """
        SELECT r.id, r.status, r.project_id, r.title, r.quantity, r.deadline, p.tenant_id
        FROM outsource_requests r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = %s
        """,
        (req_id,),
    )
    if req is None:
        raise HTTPException(status_code=404)
    if req["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)
    if req["status"] != "inviting":
        raise HTTPException(status_code=409,
                            detail=f"只有 inviting 状态可以截止；当前 '{req['status']}'")

    # 需要至少 1 个报价
    quoted_count = db.fetch_one(
        """
        SELECT COUNT(*) AS cnt FROM outsource_request_invitations
        WHERE request_id = %s AND invitation_status = 'quoted'
        """,
        (req_id,),
    )
    if not quoted_count or quoted_count["cnt"] == 0:
        raise HTTPException(status_code=400, detail="还没有任何加工方回复报价，无法截止")

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 把未回复的标 no_response
            cur.execute(
                """
                UPDATE outsource_request_invitations
                SET invitation_status = 'no_response'
                WHERE request_id = %s AND invitation_status = 'sent'
                """,
                (req_id,),
            )

            # 创建审批任务
            fake_instance_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"outsource_request:{req_id}"
            )
            node_id = f"outsource_award:{req_id}"

            # 检查是否已存在
            cur.execute(
                """
                SELECT id FROM workflow_approval_tasks
                WHERE node_id = %s AND status = 'pending'
                """,
                (node_id,),
            )
            existing = cur.fetchone()
            if existing:
                task_id = existing["id"] if isinstance(existing, dict) else existing[0]
            else:
                cur.execute(
                    """
                    INSERT INTO workflow_approval_tasks
                        (instance_id, node_id, assignee_type, assignee_id, status)
                    VALUES (%s, %s, 'role', 'ADMIN', 'pending')
                    RETURNING id
                    """,
                    (str(fake_instance_id), node_id),
                )
                task_id = cur.fetchone()[0]

                # SUBMIT record
                cur.execute(
                    """
                    INSERT INTO workflow_approval_records
                        (id, instance_id, node_id, action, actor_id,
                         assignee_type, assignee_id, comment, metadata_json, created_at)
                    VALUES (%s, %s, %s, 'submit', %s, 'role', 'ADMIN',
                            %s, %s, %s)
                    """,
                    (str(uuid.uuid4()), str(fake_instance_id), node_id,
                     str(user.user_id),
                     f"截止报价，请经理选择中标加工方: request_id={req_id}",
                     json.dumps({
                         "kind": "outsource_award",
                         "request_id": req_id,
                         "task_id": task_id,
                     }, ensure_ascii=False),
                     datetime.utcnow()),
                )

                # State event
                cur.execute(
                    """
                    INSERT INTO workflow_state_events
                        (instance_id, node_id, event_type, to_status, payload_json, occurred_at)
                    VALUES (%s, %s, 'approval_task_created', 'pending', %s, %s)
                    """,
                    (str(fake_instance_id), node_id,
                     json.dumps({"request_id": req_id, "task_id": task_id}, ensure_ascii=False),
                     datetime.utcnow()),
                )

            # 更新询价单状态
            cur.execute(
                """
                UPDATE outsource_requests
                SET status = 'pending_award', approval_task_id = %s, closed_at = %s
                WHERE id = %s
                """,
                (task_id, datetime.utcnow(), req_id),
            )

    return {"task_id": task_id, "status": "pending_award"}


# ---------------------------------------------------------------------------
# 重写审批处理 — 补充 outsource_award 逻辑
# 因为 act_approval 已经定义好了，所以这里用钩子方式扩展
# 实际上 act_approval 已经会路由 kind 的分支，但 outsource_award 的逻辑特殊（需要 awarded_invitation_id），
# 给它一个独立的端点。
# ---------------------------------------------------------------------------

@router.post("/approvals/{task_id}/award")
async def award_outsource(
    task_id: int,
    payload: AwardAction,
    user: CurrentUser = Depends(_require_internal),
):
    """采购经理为 outsource_award 类型任务执行审批。
    approve 时必须传 awarded_invitation_id。
    """
    task = db.fetch_one(
        """
        SELECT id, instance_id::text AS instance_hex, node_id,
               assignee_type, assignee_id, status
        FROM workflow_approval_tasks WHERE id = %s
        """,
        (task_id,),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Task already {task['status']}")

    node = task["node_id"] or ""
    if not node.startswith("outsource_award:"):
        raise HTTPException(status_code=400, detail="非 outsource_award 任务，请用 /action 接口")

    # 权限
    role_match = (user.role or "").upper()
    is_mine = (
        (task["assignee_type"] == "user" and task["assignee_id"] == str(user.user_id)) or
        (task["assignee_type"] == "role" and task["assignee_id"] in ("ADMIN", role_match))
    )
    if not is_mine:
        raise HTTPException(status_code=403, detail="Not your task")

    request_id = int(node.split(":", 1)[1])

    # 判断中标模式：逐项 or 整单
    use_lines = bool(payload.awarded_lines)

    # approve 校验
    if payload.action == "approve":
        if not use_lines and not payload.awarded_invitation_id:
            raise HTTPException(status_code=400,
                                detail="approve 时必须指定 awarded_invitation_id 或 awarded_lines")

    # 整单模式的数据加载
    awarded_quote = None
    if payload.action == "approve" and not use_lines:
        awarded_quote = db.fetch_one(
            """
            SELECT q.id AS quotation_id, q.unit_price, q.lead_time_days,
                   i.id AS invitation_id, i.supplier_id, i.tenant_id AS processor_tenant_id,
                   i.request_id,
                   r.quantity, r.deadline
            FROM outsource_request_invitations i
            JOIN outsource_quotations q ON q.invitation_id = i.id
            JOIN outsource_requests r ON i.request_id = r.id
            WHERE i.id = %s
            """,
            (payload.awarded_invitation_id,),
        )
        if not awarded_quote:
            raise HTTPException(status_code=400, detail="指定的邀请没有对应报价")
        if awarded_quote["request_id"] != request_id:
            raise HTTPException(status_code=400, detail="邀请不属于该询价单")

    # 逐项模式的数据加载
    awarded_line_rows = []
    if payload.action == "approve" and use_lines:
        # 查所有待中标的行 + 它们的 scope_item / 报价 / 供应商
        if not payload.awarded_lines:
            raise HTTPException(status_code=400, detail="awarded_lines 为空")
        placeholders = ",".join(["%s"] * len(payload.awarded_lines))
        awarded_line_rows = db.fetch_all(
            f"""
            SELECT ql.id AS line_id, ql.scope_item_id, ql.unit_price, ql.lead_time_days,
                   ql.note, ql.quotation_id,
                   i.id AS invitation_id, i.supplier_id, i.tenant_id AS processor_tenant_id,
                   i.request_id,
                   s.scope_level, s.quantity AS item_qty
            FROM outsource_quotation_lines ql
            JOIN outsource_quotations q ON ql.quotation_id = q.id
            JOIN outsource_request_invitations i ON q.invitation_id = i.id
            JOIN outsource_scope_items s ON ql.scope_item_id = s.id
            WHERE ql.id IN ({placeholders})
            """,
            tuple(payload.awarded_lines),
        )
        if len(awarded_line_rows) != len(payload.awarded_lines):
            raise HTTPException(status_code=400, detail="部分 quotation_line_id 找不到")
        # 校验都属于本 request
        for r in awarded_line_rows:
            if r["request_id"] != request_id:
                raise HTTPException(status_code=400,
                                    detail=f"line {r['line_id']} 不属于当前询价单")
        # 校验每个 scope_item 只选了 1 个中标
        from collections import Counter
        counts = Counter(r["scope_item_id"] for r in awarded_line_rows)
        dup = [sid for sid, c in counts.items() if c > 1]
        if dup:
            raise HTTPException(status_code=400,
                                detail=f"scope_item {dup} 不能同时选多个中标")

    now = datetime.utcnow()
    order_ids: list[int] = []
    order_nos: list[str] = []

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Append record
            action_value = payload.action
            cur.execute(
                """
                INSERT INTO workflow_approval_records
                    (id, instance_id, node_id, action, actor_id,
                     assignee_type, assignee_id, comment, metadata_json, created_at)
                VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), task["instance_hex"], node, action_value,
                 str(user.user_id), task["assignee_type"], task["assignee_id"],
                 payload.comment or "",
                 json.dumps({
                     "awarded_invitation_id": payload.awarded_invitation_id,
                     "awarded_lines": payload.awarded_lines,
                 }, ensure_ascii=False),
                 now),
            )

            # 2. Task → completed
            cur.execute(
                """
                UPDATE workflow_approval_tasks
                SET status='completed', claimed_by=%s, completed_at=%s, completion_action=%s
                WHERE id=%s
                """,
                (str(user.user_id), now, action_value, task_id),
            )

            # 3. State event
            cur.execute(
                """
                INSERT INTO workflow_state_events
                    (instance_id, node_id, event_type, from_status, to_status,
                     payload_json, occurred_at)
                VALUES (%s::uuid, %s, 'approval_action_taken', 'pending', %s, %s, %s)
                """,
                (task["instance_hex"], node,
                 "approved" if action_value == "approve" else "rejected",
                 json.dumps({
                     "actor_id": user.user_id,
                     "action": action_value,
                     "awarded_invitation_id": payload.awarded_invitation_id,
                     "awarded_lines": payload.awarded_lines,
                     "comment": payload.comment,
                 }, ensure_ascii=False),
                 now),
            )

            # 4. 业务后处理
            if action_value == "approve" and use_lines:
                # 逐项模式：每个 awarded_line 一张加工单
                for r in awarded_line_rows:
                    qty = r["item_qty"] or 1
                    unit_price = float(r["unit_price"])
                    total = unit_price * qty
                    one_order_no = _next_order_no()

                    cur.execute(
                        """
                        INSERT INTO outsource_orders
                            (request_id, quotation_id, quotation_line_id, scope_item_id,
                             supplier_id, tenant_id,
                             order_no, unit_price, quantity, total_amount,
                             lead_time_days, status, awarded_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'awarded', %s)
                        RETURNING id
                        """,
                        (request_id, r["quotation_id"], r["line_id"], r["scope_item_id"],
                         r["supplier_id"], r["processor_tenant_id"],
                         one_order_no, unit_price, qty, total,
                         r["lead_time_days"], now),
                    )
                    oid = cur.fetchone()[0]
                    order_ids.append(oid)
                    order_nos.append(one_order_no)

                    cur.execute(
                        """
                        INSERT INTO outsource_order_status_events
                            (order_id, from_status, to_status, changed_by, note, occurred_at)
                        VALUES (%s, NULL, 'awarded', %s, %s, %s)
                        """,
                        (oid, user.user_id,
                         f"采购经理批准中标 (line {r['line_id']})", now),
                    )

                    # 更新 scope_item 状态
                    cur.execute(
                        """
                        UPDATE outsource_scope_items
                        SET status = 'awarded', winning_quotation_line_id = %s
                        WHERE id = %s
                        """,
                        (r["line_id"], r["scope_item_id"]),
                    )

                cur.execute(
                    "UPDATE outsource_requests SET status='awarded' WHERE id=%s",
                    (request_id,),
                )

            elif action_value == "approve":
                # 整单模式（兼容老路径）
                qty = awarded_quote["quantity"] or 1
                unit_price = float(awarded_quote["unit_price"])
                total = unit_price * qty
                one_order_no = _next_order_no()

                cur.execute(
                    """
                    INSERT INTO outsource_orders
                        (request_id, quotation_id, supplier_id, tenant_id,
                         order_no, unit_price, quantity, total_amount,
                         lead_time_days, status, awarded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'awarded', %s)
                    RETURNING id
                    """,
                    (awarded_quote["request_id"], awarded_quote["quotation_id"],
                     awarded_quote["supplier_id"], awarded_quote["processor_tenant_id"],
                     one_order_no, unit_price, qty, total,
                     awarded_quote["lead_time_days"], now),
                )
                oid = cur.fetchone()[0]
                order_ids.append(oid)
                order_nos.append(one_order_no)

                cur.execute(
                    """
                    INSERT INTO outsource_order_status_events
                        (order_id, from_status, to_status, changed_by, note, occurred_at)
                    VALUES (%s, NULL, 'awarded', %s, '采购经理批准中标', %s)
                    """,
                    (oid, user.user_id, now),
                )

                cur.execute(
                    """
                    UPDATE outsource_requests
                    SET status='awarded', winning_quotation_id=%s
                    WHERE id=%s
                    """,
                    (awarded_quote["quotation_id"], request_id),
                )

            else:
                # reject
                cur.execute(
                    "UPDATE outsource_requests SET status='cancelled' WHERE id=%s",
                    (request_id,),
                )

            # 5. 材料供应方式传播 + 材料询价关联
            if action_value == "approve" and order_ids:
                # 从 outsource_requests 获取 material_sourcing
                cur.execute(
                    "SELECT material_sourcing FROM outsource_requests WHERE id=%s",
                    (request_id,),
                )
                ms_row = cur.fetchone()
                ms_value = ms_row[0] if ms_row else None

                if ms_value:
                    # 设置所有新建 order 的 material_sourcing
                    for oid in order_ids:
                        cur.execute(
                            """
                            UPDATE outsource_orders
                            SET material_sourcing = %s,
                                material_sourcing_decided_by = %s,
                                material_sourcing_decided_at = %s
                            WHERE id = %s
                            """,
                            (ms_value, user.user_id, now, oid),
                        )

                # 把预创建的 material_inquiries 关联到第一张 order
                # （一个 request 通常只有一张材料询价单）
                if order_ids:
                    cur.execute(
                        """
                        UPDATE material_inquiries
                        SET outsource_order_id = %s
                        WHERE outsource_request_id = %s AND outsource_order_id IS NULL
                        """,
                        (order_ids[0], request_id),
                    )

    return {
        "status": "ok",
        "action": action_value,
        "mode": "lines" if use_lines else ("single" if order_ids else "reject"),
        "outsource_order_ids": order_ids,
        "order_nos": order_nos,
    }


# ---------------------------------------------------------------------------
# 委外加工单（只读监控）
# ---------------------------------------------------------------------------

@router.get("/outsource-orders")
async def list_orders(
    user: CurrentUser = Depends(_require_internal),
    status_filter: Optional[str] = None,
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if status_filter:
        where += " AND o.status = %s"
        params.append(status_filter)

    rows = db.fetch_all(
        f"""
        SELECT o.id, o.order_no, o.unit_price, o.quantity, o.total_amount,
               o.lead_time_days, o.status, o.material_sourcing,
               o.awarded_at, o.accepted_at, o.delivered_at, o.created_at,
               s.name AS supplier_name,
               p.project_no, p.name AS project_name,
               r.title AS request_title
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        JOIN suppliers s ON o.supplier_id = s.id
        {where}
        ORDER BY o.created_at DESC
        LIMIT 50
        """,
        tuple(params),
    )
    for r in rows:
        if r.get("unit_price"): r["unit_price"] = float(r["unit_price"])
        if r.get("total_amount"): r["total_amount"] = float(r["total_amount"])
        for k in ("awarded_at", "accepted_at", "delivered_at", "created_at"):
            if r.get(k):
                r[k] = r[k].isoformat() if hasattr(r[k], "isoformat") else r[k]
    return {"items": rows, "total": len(rows)}


@router.get("/outsource-orders/{order_id}")
async def get_order(
    order_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    row = db.fetch_one(
        """
        SELECT o.*, s.name AS supplier_name, s.contact_name, s.contact_phone,
               p.tenant_id, p.project_no, p.name AS project_name,
               r.title AS request_title, r.required_processes_json
        FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id = r.id
        JOIN projects p ON r.project_id = p.id
        JOIN suppliers s ON o.supplier_id = s.id
        WHERE o.id = %s
        """,
        (order_id,),
    )
    if row is None:
        raise HTTPException(status_code=404)
    if row["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=403)

    raw = row.get("required_processes_json")
    row["required_processes"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    row.pop("required_processes_json", None)

    events = db.fetch_all(
        """
        SELECT from_status, to_status, changed_by, note, occurred_at
        FROM outsource_order_status_events
        WHERE order_id = %s ORDER BY occurred_at ASC
        """,
        (order_id,),
    )
    for e in events:
        if e.get("occurred_at"):
            e["occurred_at"] = e["occurred_at"].isoformat()

    # 关联的材料方发货照片
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

    return {
        "order": row,
        "events": events,
        "shipments_detail": ship_rows,
    }



# =============================================================================
# Stage 4 自制分支：查看/管理自制生产单 + 给材料方的采购单 (PO)
# =============================================================================

class POCreate(BaseModel):
    project_id: int
    mold_id: Optional[int] = None
    supplier_id: int
    material_code: str
    spec: Optional[str] = None
    qty: float = Field(..., gt=0)
    unit: str = "kg"
    unit_price: Optional[float] = None
    required_date: Optional[str] = None
    remark: Optional[str] = None


class ReceivePayload(BaseModel):
    shipment_id: int
    received_qty: Optional[float] = None       # 省略时取 shipment.qty_shipped
    remark: Optional[str] = None


def _next_po_no(project_no: str) -> str:
    """生成采购单号：MP-<project_no>-<random 4>."""
    return f"MP-{project_no}-{uuid.uuid4().hex[:4].upper()}"


def _apply_moq_rule(material_code: str, spec: Optional[str],
                    supplier_id: int, qty: float) -> tuple[float, float, str | None]:
    """根据 MOQ 规则调整数量。返回 (adj_qty, surplus_qty, policy)。
    未命中规则时返回 (qty, 0, None)。
    """
    rule = db.fetch_one(
        """
        SELECT min_qty, multiple_of, surplus_policy
        FROM moq_rules
        WHERE material_code = %s
          AND COALESCE(spec, '') = COALESCE(%s, '')
          AND (supplier_id IS NULL OR supplier_id = %s)
          AND is_active = TRUE
        ORDER BY supplier_id NULLS LAST
        LIMIT 1
        """,
        (material_code, spec, supplier_id),
    )
    if rule is None:
        return qty, 0.0, None

    min_q = rule["min_qty"] or 1
    mul   = rule["multiple_of"] or 1
    adj   = max(qty, min_q)
    # 向上取到 multiple_of
    if mul > 1:
        remainder = adj % mul
        if remainder > 0:
            adj = adj + (mul - remainder)
    surplus = max(0.0, adj - qty)
    return float(adj), float(surplus), rule["surplus_policy"]


@router.get("/production-orders")
async def list_internal_orders(
    project_id: Optional[int] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if project_id is not None:
        where += " AND ipo.project_id = %s"
        params.append(project_id)

    rows = db.fetch_all(
        f"""
        SELECT ipo.*, p.project_no, p.name AS project_name,
               pp.part_no, pp.part_name,
               pr.process_code, pr.method_name,
               m.mold_no
        FROM internal_production_orders ipo
        JOIN projects p ON ipo.project_id = p.id
        LEFT JOIN project_parts pp ON ipo.part_id = pp.id
        LEFT JOIN project_processes pr ON ipo.process_id = pr.id
        LEFT JOIN molds m ON ipo.mold_id = m.id
        {where}
        ORDER BY ipo.created_at DESC
        """,
        tuple(params),
    )
    return {"items": rows, "total": len(rows)}


@router.get("/material-pos")
async def list_material_pos(
    project_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    user: CurrentUser = Depends(_require_internal),
):
    where = "WHERE p.tenant_id = %s"
    params: list[Any] = [user.tenant_id]
    if project_id is not None:
        where += " AND mpo.project_id = %s"
        params.append(project_id)
    if supplier_id is not None:
        where += " AND mpo.supplier_id = %s"
        params.append(supplier_id)

    rows = db.fetch_all(
        f"""
        SELECT mpo.*, p.project_no, p.name AS project_name,
               s.name AS supplier_name,
               m.mold_no
        FROM material_purchase_orders mpo
        JOIN projects p ON mpo.project_id = p.id
        JOIN suppliers s ON mpo.supplier_id = s.id
        LEFT JOIN molds m ON mpo.mold_id = m.id
        {where}
        ORDER BY mpo.created_at DESC
        LIMIT 100
        """,
        tuple(params),
    )
    for r in rows:
        for k in ("unit_price", "total_amount", "qty", "moq_surplus_qty"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"items": rows, "total": len(rows)}


@router.post("/material-pos", status_code=201)
async def create_material_po(
    payload: POCreate,
    user: CurrentUser = Depends(_require_internal),
):
    """新建采购单：会自动应用 MOQ 规则（超量写入 moq_surplus_qty）。"""
    proj = db.fetch_one(
        "SELECT id, project_no, tenant_id FROM projects WHERE id=%s",
        (payload.project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")

    # MOQ 规则
    adj_qty, surplus, policy = _apply_moq_rule(
        payload.material_code, payload.spec, payload.supplier_id, payload.qty,
    )
    total = (adj_qty * payload.unit_price) if payload.unit_price else None
    po_no = _next_po_no(proj["project_no"])

    # 找材料方租户 id
    tenant = db.fetch_one(
        "SELECT id FROM tenants WHERE supplier_id=%s AND tenant_type='material' LIMIT 1",
        (payload.supplier_id,),
    )

    new_id = db.execute(
        """
        INSERT INTO material_purchase_orders
            (po_no, project_id, mold_id, supplier_id, tenant_id,
             material_code, spec, qty, unit, unit_price, total_amount,
             required_date, status, created_by, moq_surplus_qty, remark)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'sent', %s, %s, %s)
        RETURNING id
        """,
        (po_no, payload.project_id, payload.mold_id, payload.supplier_id,
         tenant["id"] if tenant else None,
         payload.material_code, payload.spec, adj_qty, payload.unit,
         payload.unit_price, total,
         payload.required_date, user.user_id, surplus, payload.remark),
    )

    return {
        "id": new_id, "po_no": po_no,
        "qty": adj_qty, "moq_surplus_qty": surplus,
        "moq_policy": policy,
    }


@router.post("/material-pos/{po_id}/receive")
async def receive_po(
    po_id: int,
    payload: ReceivePayload,
    user: CurrentUser = Depends(_require_internal),
):
    """我方确认收料 → 更新订单/发货状态 → MOQ 余量入库。"""
    po = db.fetch_one(
        """
        SELECT mpo.*, p.tenant_id
        FROM material_purchase_orders mpo
        JOIN projects p ON mpo.project_id = p.id
        WHERE mpo.id = %s
        """,
        (po_id,),
    )
    if po is None or po["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    ship = db.fetch_one(
        "SELECT * FROM material_shipments WHERE id = %s AND po_id = %s",
        (payload.shipment_id, po_id),
    )
    if ship is None:
        raise HTTPException(status_code=400, detail="shipment 不属于该订单")
    if ship["status"] not in ("shipped",):
        raise HTTPException(status_code=409, detail=f"shipment 当前 '{ship['status']}' 不能确认收料")

    now = datetime.utcnow()
    received = payload.received_qty if payload.received_qty is not None else ship["qty_shipped"]

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE material_shipments SET status='received', received_at=%s, remark=COALESCE(%s, remark) WHERE id=%s",
                (now, payload.remark, payload.shipment_id),
            )
            cur.execute(
                "UPDATE material_purchase_orders SET status='received' WHERE id=%s",
                (po_id,),
            )

            # MOQ 余量入库：仅当 moq_surplus_qty > 0 且 policy=stock
            surplus_qty = float(po["moq_surplus_qty"] or 0)
            if surplus_qty > 0:
                # 计算收到的实际 surplus（保守：如果 received < adj_qty，按比例）
                adj_qty = float(po["qty"])
                effective_surplus = max(0.0, float(received) - (adj_qty - surplus_qty))
                if effective_surplus > 0:
                    cur.execute(
                        """
                        INSERT INTO inventory
                            (material_code, spec, batch_no, qty, unit,
                             inventory_type, source_type, source_id,
                             warehouse_location, remark)
                        VALUES (%s, %s, %s, %s, %s, 'moq_surplus', 'material_shipment', %s,
                                NULL, %s)
                        """,
                        (po["material_code"], po["spec"], ship["batch_no"],
                         effective_surplus, po["unit"],
                         payload.shipment_id,
                         f"PO {po['po_no']} 的 MOQ 余量自动入库"),
                    )

    return {
        "status": "received",
        "received_qty": received,
        "moq_surplus_stocked": surplus_qty > 0,
    }


# =============================================================================
# 工具：审批 approve 生产决策 → 自动生成自制单
# =============================================================================
# NOTE: 实际触发点在审批 act_approval 里（已经有项目状态变更逻辑）。
#       这里只提供一个独立接口供手动触发/测试。

@router.post("/projects/{project_id}/generate-self-orders")
async def generate_self_orders(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """按 production_decisions.final_decision='self_made' 的行批量生成 internal_production_orders。

    幂等：同一 (project_id, part_id, process_id) 只生成一张。
    """
    proj = db.fetch_one(
        "SELECT id, project_no, tenant_id FROM projects WHERE id=%s",
        (project_id,),
    )
    if proj is None or proj["tenant_id"] != user.tenant_id:
        raise HTTPException(status_code=404)

    decisions = db.fetch_all(
        """
        SELECT d.id, d.part_id, d.process_id,
               pp.mold_id, pp.qty AS part_qty
        FROM production_decisions d
        JOIN project_parts pp ON d.part_id = pp.id
        WHERE d.project_id = %s AND d.final_decision = 'self_made'
        """,
        (project_id,),
    )
    if not decisions:
        return {"created": 0, "note": "没有自制决策行"}

    now = datetime.utcnow()
    created = 0
    skipped = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for d in decisions:
                # 幂等检查
                cur.execute(
                    """
                    SELECT id FROM internal_production_orders
                    WHERE project_id=%s AND part_id=%s
                      AND COALESCE(process_id, 0) = COALESCE(%s, 0)
                    LIMIT 1
                    """,
                    (project_id, d["part_id"], d["process_id"]),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                # 查 part_no / process_code
                cur.execute(
                    "SELECT part_no FROM project_parts WHERE id=%s",
                    (d["part_id"],),
                )
                part_no = cur.fetchone()[0] if cur.rowcount else "part"
                proc_suffix = ""
                if d["process_id"]:
                    cur.execute(
                        "SELECT process_code FROM project_processes WHERE id=%s",
                        (d["process_id"],),
                    )
                    r = cur.fetchone()
                    if r:
                        proc_suffix = f"-{r[0]}"

                order_no = f"IP-{proj['project_no']}-{part_no}{proc_suffix}-{created+1:02d}"

                cur.execute(
                    """
                    INSERT INTO internal_production_orders
                        (order_no, project_id, mold_id, part_id, process_id,
                         qty, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                    """,
                    (order_no, project_id, d["mold_id"], d["part_id"], d["process_id"],
                     d["part_qty"] or 1),
                )
                created += 1

    return {"created": created, "skipped": skipped}



# =============================================================================
# Stage 7-KANBAN：全流程节点追踪
# =============================================================================

from mvp import tracking as _tracking


class HoldPayload(BaseModel):
    reason: str
    node_code: str


@router.get("/tracking")
async def list_tracking_all(user: CurrentUser = Depends(_require_internal)):
    """看板：所有项目 × 10 节点的状态矩阵。"""
    rows = db.fetch_all(
        """
        SELECT wt.project_id, wt.node_code, wt.node_name, wt.node_order,
               wt.status, wt.started_at, wt.ended_at, wt.duration_hours,
               wt.is_blocking, wt.blocker_reason, wt.remark,
               p.project_no, p.name AS project_name, p.status AS project_status,
               p.customer, p.deadline
        FROM workflow_tracking wt
        JOIN projects p ON wt.project_id = p.id
        WHERE p.tenant_id = %s
        ORDER BY p.id DESC, wt.node_order ASC
        """,
        (user.tenant_id,),
    )
    # 按 project 分组
    projects: dict[int, dict] = {}
    for r in rows:
        pid = r["project_id"]
        if pid not in projects:
            projects[pid] = {
                "project_id":    pid,
                "project_no":    r["project_no"],
                "project_name":  r["project_name"],
                "project_status":r["project_status"],
                "customer":      r["customer"],
                "deadline":      r["deadline"],
                "nodes": [],
            }
        if r.get("duration_hours") is not None:
            r["duration_hours"] = float(r["duration_hours"])
        projects[pid]["nodes"].append({
            "code":   r["node_code"],
            "name":   r["node_name"],
            "order":  r["node_order"],
            "status": r["status"],
            "started_at":     r["started_at"],
            "ended_at":       r["ended_at"],
            "duration_hours": r["duration_hours"],
            "is_blocking":    r["is_blocking"],
            "blocker_reason": r["blocker_reason"],
            "remark":         r["remark"],
        })

    return {"projects": list(projects.values()), "total": len(projects)}


@router.post("/tracking/{project_id}/sync")
async def sync_tracking(
    project_id: int,
    user: CurrentUser = Depends(_require_internal),
):
    """根据现有数据回填/刷新 workflow_tracking 行（幂等）。"""
    proj = db.fetch_one(
        "SELECT id, status FROM projects WHERE id=%s AND tenant_id=%s",
        (project_id, user.tenant_id),
    )
    if proj is None:
        raise HTTPException(status_code=404)

    _tracking.ensure_nodes(project_id)

    # 1) drafting / confirmed / deciding / decided 基于 project.status
    status = proj["status"]
    order_map = {"drafted":1, "confirmed":2, "deciding":3, "decided":4, "completed":10, "cancelled":0}
    code_flow = ["drafting","confirmed","deciding","decided"]
    cur_pos = order_map.get(status, 1)
    for i, code in enumerate(code_flow, start=1):
        if i < cur_pos:
            _tracking.track(project_id, code, "done", user.user_id)
        elif i == cur_pos:
            _tracking.track(project_id, code, "in_progress" if status != "decided" else "done", user.user_id)

    # 2) purchasing / outsourcing 基于有没有相关订单
    has_mpo = db.fetch_one(
        "SELECT 1 FROM material_purchase_orders WHERE project_id=%s LIMIT 1",
        (project_id,),
    )
    if has_mpo:
        _tracking.track(project_id, "purchasing", "in_progress", user.user_id)
        all_received = db.fetch_one(
            """
            SELECT CASE WHEN COUNT(*) FILTER (WHERE status != 'received') = 0 THEN 1 ELSE 0 END AS ok
            FROM material_purchase_orders WHERE project_id=%s
            """,
            (project_id,),
        )
        if all_received and all_received["ok"]:
            _tracking.track(project_id, "purchasing", "done", user.user_id)

    has_oo = db.fetch_one(
        """
        SELECT 1 FROM outsource_orders o
        JOIN outsource_requests r ON o.request_id=r.id
        WHERE r.project_id=%s LIMIT 1
        """,
        (project_id,),
    )
    if has_oo:
        _tracking.track(project_id, "outsourcing", "in_progress", user.user_id)
        all_delivered = db.fetch_one(
            """
            SELECT CASE WHEN COUNT(*) FILTER (WHERE o.status != 'delivered') = 0 THEN 1 ELSE 0 END AS ok
            FROM outsource_orders o
            JOIN outsource_requests r ON o.request_id=r.id
            WHERE r.project_id=%s
            """,
            (project_id,),
        )
        if all_delivered and all_delivered["ok"]:
            _tracking.track(project_id, "outsourcing", "done", user.user_id)

    # 3) producing 基于 internal_production_orders
    has_ipo = db.fetch_one(
        "SELECT 1 FROM internal_production_orders WHERE project_id=%s LIMIT 1",
        (project_id,),
    )
    if has_ipo:
        _tracking.track(project_id, "producing", "in_progress", user.user_id)
        all_finished = db.fetch_one(
            """
            SELECT CASE WHEN COUNT(*) FILTER (WHERE status != 'finished') = 0 THEN 1 ELSE 0 END AS ok
            FROM internal_production_orders WHERE project_id=%s
            """,
            (project_id,),
        )
        if all_finished and all_finished["ok"]:
            _tracking.track(project_id, "producing", "done", user.user_id)

    # 4) inspecting 基于 inspections 是否存在
    has_insp = db.fetch_one("SELECT 1 FROM inspections WHERE project_id=%s LIMIT 1",
                             (project_id,))
    if has_insp:
        _tracking.track(project_id, "inspecting", "in_progress", user.user_id)
        has_pass = db.fetch_one(
            "SELECT 1 FROM inspections WHERE project_id=%s AND result='pass' LIMIT 1",
            (project_id,),
        )
        if has_pass:
            _tracking.track(project_id, "inspecting", "done", user.user_id)

    # 5) exception 基于 quality_exceptions
    has_exc_open = db.fetch_one(
        """
        SELECT 1 FROM quality_exceptions
        WHERE project_id=%s AND status NOT IN ('resolved','closed','cancelled')
        LIMIT 1
        """,
        (project_id,),
    )
    has_exc_closed = db.fetch_one(
        """
        SELECT 1 FROM quality_exceptions
        WHERE project_id=%s AND status IN ('resolved','closed')
        LIMIT 1
        """,
        (project_id,),
    )
    if has_exc_open:
        _tracking.track(project_id, "exception", "in_progress", user.user_id)
    elif has_exc_closed:
        _tracking.track(project_id, "exception", "done", user.user_id)

    return {"synced": True, "project_id": project_id}


@router.post("/tracking/{project_id}/hold")
async def hold_node(
    project_id: int,
    payload: HoldPayload,
    user: CurrentUser = Depends(_require_internal),
):
    proj = db.fetch_one(
        "SELECT id FROM projects WHERE id=%s AND tenant_id=%s",
        (project_id, user.tenant_id),
    )
    if proj is None: raise HTTPException(status_code=404)
    _tracking.hold(project_id, payload.node_code, payload.reason, user.user_id)
    return {"status": "on_hold"}

