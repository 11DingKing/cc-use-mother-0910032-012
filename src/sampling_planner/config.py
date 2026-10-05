"""静态配置：原因码、状态机、容量常量。"""
from __future__ import annotations

# ---------- 机构 ----------
INSTITUTION_STATUS = ("营业", "停业")

# ---------- 风险等级 ----------
RISK_LEVELS = ("高", "中", "低")

# ---------- 计划状态 ----------
# 草稿(候选生成中) -> 候选就绪 -> 已确认(已锁定资源) -> [已发布, 已归档, 已取消]
PLAN_DRAFT = "草稿"
PLAN_CANDIDATES_READY = "候选就绪"
PLAN_CONFIRMED = "已确认"
PLAN_PUBLISHED = "已发布"
PLAN_ARCHIVED = "已归档"
PLAN_CANCELLED = "已取消"

PLAN_STATES = (
    PLAN_DRAFT,
    PLAN_CANDIDATES_READY,
    PLAN_CONFIRMED,
    PLAN_PUBLISHED,
    PLAN_ARCHIVED,
    PLAN_CANCELLED,
)

# 允许的状态迁移
PLAN_TRANSITIONS = {
    PLAN_DRAFT: {PLAN_CANDIDATES_READY, PLAN_CANCELLED},
    PLAN_CANDIDATES_READY: {PLAN_CONFIRMED, PLAN_DRAFT, PLAN_CANCELLED},
    PLAN_CONFIRMED: {PLAN_PUBLISHED, PLAN_ARCHIVED},
    PLAN_PUBLISHED: {PLAN_ARCHIVED},
    PLAN_ARCHIVED: set(),
    PLAN_CANCELLED: set(),
}

# ---------- 计划项状态 ----------
ITEM_PENDING = "待检"
ITEM_RESCHEDULED = "已改期"
ITEM_INSPECTOR_SWAPPED = "已换人"
ITEM_DROPPED = "已剔除"  # 机构停业后，确认后的计划项被剔除并保留版本

ITEM_STATES = (
    ITEM_PENDING,
    ITEM_RESCHEDULED,
    ITEM_INSPECTOR_SWAPPED,
    ITEM_DROPPED,
)

# ---------- 候选解释原因码 ----------
# 入选
REASON_SELECTED = "SELECTED"                 # 按规则入选，已在候选中预分配
# 未入选
REASON_RULE_FILTERED = "RULE_FILTERED"       # 不满足任何启用规则
REASON_BELOW_CUTOFF = "BELOW_CUTOFF"         # 满足规则但排序靠后，超过目标数量
REASON_REGION_CAPACITY = "REGION_CAPACITY"   # 区域季度容量已满
REASON_NO_QUALIFIED_INSPECTOR = "NO_QUALIFIED_INSPECTOR"  # 无可用合格检查员
REASON_AVOIDANCE = "AVOIDANCE"               # 合格检查员均与本机构存在回避
REASON_INSPECTOR_CAPACITY = "INSPECTOR_CAPACITY"          # 检查员季度槽位均已占用
REASON_INSTITUTION_SUSPENDED = "INSTITUTION_SUSPENDED"    # 机构停业
REASON_ALREADY_PLANNED = "ALREADY_PLANNED"   # 本季度已被其他计划锁定
REASON_SNAPSHOT_STALE = "SNAPSHOT_STALE"     # 风险快照过期，按规则不得入选

REASON_TEXT = {
    REASON_SELECTED: "符合规则「{rule}」，风险排序入选，预分配检查员 {inspector}",
    REASON_RULE_FILTERED: "不满足任何启用的抽检规则（风险等级/最低评分/行业/标签条件均未命中）",
    REASON_BELOW_CUTOFF: "符合规则「{rule}」，但风险排序靠后，规则名额 {quota} 名已满",
    REASON_REGION_CAPACITY: "所在区域「{region}」季度容量 {cap} 个已被占用",
    REASON_NO_QUALIFIED_INSPECTOR: "规则要求资格「{qualification}」，无具备该资格且未停用的检查员",
    REASON_AVOIDANCE: "具备资格的检查员均与本机构存在回避关系（{names}）",
    REASON_INSPECTOR_CAPACITY: "无回避的合格检查员季度槽位均已被其他任务占用",
    REASON_INSTITUTION_SUSPENDED: "机构当前为停业状态，不予抽检",
    REASON_ALREADY_PLANNED: "本季度机构已被计划 {plan} 锁定，禁止重复占用",
    REASON_SNAPSHOT_STALE: "风险快照已过期（超过 {days} 天），数据不可信",
}

# ---------- 锁类型 ----------
LOCK_INSTITUTION = "institution"   # 机构季度唯一占用
LOCK_INSPECTOR = "inspector"       # 检查员季度槽位（容量 N）
LOCK_REGION = "region"             # 区域季度槽位（容量 N）

# ---------- 快照 ----------
DEFAULT_SNAPSHOT_MAX_AGE_DAYS = 90
