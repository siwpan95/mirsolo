"""技能子系统（P1-S1：学习/MP/冷却/距离门 + 伤害与治疗路由）。"""

from mirsolo.skills.engine import (
    ALL_KINDS,
    ATTACK_KINDS,
    NON_ATTACK_KINDS,
    EVT_SKILL_CAST,
    EVT_SKILL_REJECTED,
    KEY_SKILL_INTENTS,
    MAX_PROFICIENCY,
    PLANNED_KINDS,
    SELF_KINDS,
    SUPPORTED_KINDS,
    SkillError,
    SkillSystem,
    can_cast,
    learn,
    queue_skill,
)

__all__ = [
    "ALL_KINDS",
    "ATTACK_KINDS",
    "NON_ATTACK_KINDS",
    "EVT_SKILL_CAST",
    "EVT_SKILL_REJECTED",
    "KEY_SKILL_INTENTS",
    "MAX_PROFICIENCY",
    "PLANNED_KINDS",
    "SELF_KINDS",
    "SUPPORTED_KINDS",
    "SkillError",
    "SkillSystem",
    "can_cast",
    "learn",
    "queue_skill",
]
