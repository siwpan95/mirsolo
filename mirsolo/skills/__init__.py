"""技能子系统（P1-S1：学习/MP/冷却/距离门 + 伤害与治疗路由）。"""

from mirsolo.skills.engine import (
    ALL_KINDS,
    EVT_SKILL_CAST,
    KEY_SKILL_INTENTS,
    PLANNED_KINDS,
    SUPPORTED_KINDS,
    SkillError,
    SkillSystem,
    can_cast,
    learn,
    queue_skill,
)

__all__ = [
    "ALL_KINDS",
    "EVT_SKILL_CAST",
    "KEY_SKILL_INTENTS",
    "PLANNED_KINDS",
    "SUPPORTED_KINDS",
    "SkillError",
    "SkillSystem",
    "can_cast",
    "learn",
    "queue_skill",
]
