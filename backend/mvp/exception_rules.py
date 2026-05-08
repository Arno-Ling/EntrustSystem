"""
硬编码的"异常分析"规则引擎（模拟 ExceptionAgent）。

未来升级路径：把 `analyze_exception()` 替换为真实的 LLM 调用，
签名和返回结构不变即可无缝切换到 ExceptionAgent。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ExceptionAnalysis:
    """异常分析报告（结构化）。"""
    severity: str                        # low / medium / high / critical
    probable_cause: str                  # 根因文字描述
    suggested_party: str                 # material_supplier / processor / internal / customer / shared
    suggested_ratio: float               # 责任比例 0-100（shared 时 <100）
    suggested_path: str                  # rework_material / rework_process / concession / claim
    evidence_required: list[str]         # 需要收集的证据类型
    reasoning: str                       # 推理链
    confidence: float                    # 0-1

    def to_dict(self) -> dict:
        return asdict(self)


# 异常类型 → 责任方映射（经验规则）
_DEFECT_PARTY_MAP = {
    # 材料方责任
    "材料批次不良":   ("material_supplier", "rework_material"),
    "材质不符":        ("material_supplier", "rework_material"),
    "材料尺寸超差":   ("material_supplier", "rework_material"),
    "材料硬度异常":   ("material_supplier", "rework_material"),

    # 加工方责任
    "尺寸超差":        ("processor", "rework_process"),
    "外观缺陷":        ("processor", "rework_process"),
    "划伤":            ("processor", "rework_process"),
    "磕碰":            ("processor", "rework_process"),
    "热处理不均":      ("processor", "rework_process"),
    "表面处理缺陷":    ("processor", "rework_process"),
    "粗糙度不达标":    ("processor", "rework_process"),

    # 我方责任（图纸/工艺设计）
    "图纸错误":        ("internal", "rework_process"),
    "工艺设计错误":    ("internal", "rework_process"),

    # 客户变更
    "客户变更":        ("customer", "concession"),
}


# 严重度评估（按缺陷比例）
def _severity(sample_qty: int, defect_qty: int) -> str:
    if not sample_qty:
        return "medium"
    ratio = defect_qty / sample_qty
    if ratio >= 0.5:
        return "critical"
    if ratio >= 0.2:
        return "high"
    if ratio >= 0.05:
        return "medium"
    return "low"


def analyze_exception(
    *,
    defect_type: Optional[str],
    sample_qty: int,
    defect_qty: int,
    subject_type: str,           # material / internal_production / outsource / final
    notes: Optional[str] = None,
) -> ExceptionAnalysis:
    """根据质检结果生成异常分析建议（硬编码模拟 AI）。"""

    # 1) 严重度
    severity = _severity(sample_qty, defect_qty)

    # 2) 责任方推断
    party = "internal"          # 默认
    path  = "concession"
    cause = "暂无明确定责依据，请人工判定"

    if defect_type and defect_type in _DEFECT_PARTY_MAP:
        party, path = _DEFECT_PARTY_MAP[defect_type]
        cause = f"「{defect_type}」类缺陷，经验规则定责"
    else:
        # 按 subject_type 兜底
        if subject_type == "material":
            party, path = "material_supplier", "rework_material"
            cause = "来料质检不合格，初判材料方责任"
        elif subject_type == "outsource":
            party, path = "processor", "rework_process"
            cause = "外协加工件不合格，初判加工方责任"
        elif subject_type == "internal_production":
            party, path = "internal", "rework_process"
            cause = "自制加工件不合格，我方责任"
        elif subject_type == "final":
            party, path = "shared", "concession"
            cause = "成品出厂检不合格，涉及多环节，建议多方共担"

    # 3) 严重时升级：critical 改索赔
    if severity == "critical" and path == "rework_process":
        path = "claim"
        cause += "（不良率过高，升级为索赔）"

    # 4) 证据要求
    evidence_required = ["inspection_report", "drawing"]
    if subject_type in ("material",):
        evidence_required.append("iqc_report")
    if subject_type in ("outsource", "final"):
        evidence_required.append("photo")
    if path == "claim":
        evidence_required.append("cost_breakdown")

    # 5) 分摊比例（shared 时 50/50，否则 100）
    ratio = 50.0 if party == "shared" else 100.0

    # 6) 推理链
    reasoning = (
        f"质检阶段={subject_type}；样本/不良={sample_qty}/{defect_qty}，"
        f"严重度={severity}；缺陷类型={defect_type or '未分类'}；"
        f"→ 推断责任方={party}，处理路径={path}"
    )

    # 7) 置信度（规则匹配命中则高，兜底则低）
    confidence = 0.85 if defect_type in _DEFECT_PARTY_MAP else 0.55

    return ExceptionAnalysis(
        severity=severity,
        probable_cause=cause,
        suggested_party=party,
        suggested_ratio=ratio,
        suggested_path=path,
        evidence_required=evidence_required,
        reasoning=reasoning,
        confidence=confidence,
    )
