"""领域数据结构。

所有写操作都以版本化快照落库；这些 dataclass 只描述某一时刻的读取视图，
不携带可变状态，因此可以安全地在候选生成与接口序列化之间传递。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 计划生命周期（在领域契约五状态上的细化，名称保持对应）：
# 草稿(登记) -> 候选待确认(待核验) -> 已锁定(处置中) -> 已发布(已决定) -> 已归档
PLAN_DRAFT = "草稿"
PLAN_CANDIDATE = "候选待确认"
PLAN_LOCKED = "已锁定"
PLAN_PUBLISHED = "已发布"
PLAN_ARCHIVED = "已归档"

PLAN_STATES = (PLAN_DRAFT, PLAN_CANDIDATE, PLAN_LOCKED, PLAN_PUBLISHED, PLAN_ARCHIVED)

# 任务状态
TASK_PROPOSED = "候选"
TASK_LOCKED = "已锁定"
TASK_RESCHEDULED = "已改期"
TASK_REPLACED = "已换人"
TASK_CANCELLED = "已取消"  # 机构停业等

# 候选判定
DECISION_SELECTED = "入选"
DECISION_REJECTED = "未入选"


@dataclass(frozen=True)
class RiskSnapshot:
    """机构风险快照（按版本保留）。"""

    institution_id: str
    version: int
    risk_level: str          # 高 / 中 / 低
    risk_score: float        # 0~100，评分模型输出
    risk_factors: list[str]  # 可解释因子，如 ["近12个月投诉:8", "上次抽检不合格"]
    active: bool             # 快照时机构是否营业
    snapshot_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "version": self.version,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "risk_factors": list(self.risk_factors),
            "active": self.active,
            "snapshot_note": self.snapshot_note,
        }


@dataclass(frozen=True)
class Institution:
    """机构主档 + 最新风险快照的聚合视图。"""

    institution_id: str
    name: str
    region: str
    active: bool
    closed_version: int | None
    risk: RiskSnapshot | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "name": self.name,
            "region": self.region,
            "active": self.active,
            "closed_version": self.closed_version,
            "risk": None if self.risk is None else self.risk.to_dict(),
        }


@dataclass(frozen=True)
class Inspector:
    """检查员及其资格（资格按版本保留）。"""

    inspector_id: str
    name: str
    qualifications: list[str]  # 可承担的检查类型，如 ["财务", "消防"]
    regions: list[str]         # 可服务区域；["*"] 表示全域
    active: bool
    version: int
    quarterly_capacity: int = 2  # 每季度最多承担检查任务数（有限检查力量）

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspector_id": self.inspector_id,
            "name": self.name,
            "qualifications": list(self.qualifications),
            "regions": list(self.regions),
            "active": self.active,
            "version": self.version,
            "quarterly_capacity": self.quarterly_capacity,
        }


@dataclass(frozen=True)
class Recusal:
    """临时回避关系（检查员 -> 机构），按版本保留。

    ``lifted`` 为 True 表示该版本撤销回避（回避解除同样留痕）。
    """

    inspector_id: str
    institution_id: str
    reason: str
    version: int
    lifted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspector_id": self.inspector_id,
            "institution_id": self.institution_id,
            "reason": self.reason,
            "version": self.version,
            "lifted": self.lifted,
        }


@dataclass(frozen=True)
class RegionCapacity:
    """区域单季度可承载检查任务数上限，按版本保留。"""

    region: str
    capacity: int
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {"region": self.region, "capacity": self.capacity, "version": self.version}


@dataclass(frozen=True)
class Assignment:
    """计划内的一条检查任务（候选或已锁定）。"""

    institution_id: str
    institution_name: str
    region: str
    risk_level: str
    risk_score: float
    inspector_id: str | None
    inspector_name: str | None
    status: str
    reasons: list[str]
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "institution_name": self.institution_name,
            "region": self.region,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "inspector_id": self.inspector_id,
            "inspector_name": self.inspector_name,
            "status": self.status,
            "reasons": list(self.reasons),
            "history": list(self.history),
        }


@dataclass(frozen=True)
class Candidate:
    """机构级候选结论：入选或未入选，均带理由。"""

    institution_id: str
    institution_name: str
    region: str
    risk_level: str
    risk_score: float
    decision: str
    reasons: list[str]
    proposed_inspector_id: str | None
    proposed_inspector_name: str | None
    eligible_inspector_ids: list[str]
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "institution_name": self.institution_name,
            "region": self.region,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "proposed_inspector_id": self.proposed_inspector_id,
            "proposed_inspector_name": self.proposed_inspector_name,
            "eligible_inspector_ids": list(self.eligible_inspector_ids),
            "rank": self.rank,
        }


@dataclass(frozen=True)
class Plan:
    """抽检计划读视图。assignments 为当前版本任务，versions 为历史版本号。"""

    plan_id: str
    quarter: str
    inspection_type: str
    status: str
    version: int
    progress: str
    created_at: str
    updated_at: str
    assignments: tuple[Assignment, ...] = ()
    versions: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "quarter": self.quarter,
            "inspection_type": self.inspection_type,
            "status": self.status,
            "version": self.version,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "assignments": [a.to_dict() for a in self.assignments],
            "versions": list(self.versions),
        }
