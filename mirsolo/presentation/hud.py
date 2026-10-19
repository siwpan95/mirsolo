"""HUD 布局 —— 纯几何计算，零 GL。

把"血蓝条该多宽、文字该放哪"算成矩形与文本锚点，渲染层照着画。
这样布局可以在无窗口环境里单测：比例是否钳制、条是否出屏、
经验条是否按当前等级阈值算——都是能断言的数字，而不是"看着差不多"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mirsolo.components import Bag, Progression, Vitals, Wallet
from mirsolo.data.loader import GameData

RGBA = tuple[float, float, float, float]


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def top(self) -> float:
        return self.y + self.h

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.top


@dataclass(frozen=True)
class Bar:
    """一根状态条：底槽 + 填充（fill 宽度已按比例算好）。"""

    frame: Rect
    fill: Rect
    ratio: float
    color: RGBA
    caption: str = ""

    @property
    def empty(self) -> bool:
        return self.fill.w <= 0.0


@dataclass(frozen=True)
class TextAnchor:
    text: str
    x: float
    y: float
    size: int = 12
    color: RGBA = (0.92, 0.92, 0.92, 1.0)


@dataclass(frozen=True)
class SkillSlot:
    """技能栏一格：按键号 + 名称 + 是否就绪。"""

    key: str
    name: str
    rect: Rect
    ready: bool
    proficiency: int = 1


@dataclass
class HudLayout:
    hp: Bar
    mp: Bar
    exp: Bar
    texts: list[TextAnchor] = field(default_factory=list)
    panels: dict[str, Rect] = field(default_factory=dict)
    skills: list[SkillSlot] = field(default_factory=list)
    """已学技能栏（最多 5 格，按技能 id 稳定排序，与学习顺序无关）。"""


def clamp_ratio(value: float, maximum: float) -> float:
    """比例钳制到 [0,1]；``maximum <= 0`` 时视为 0（避免除零与"满格假象"）。"""
    if maximum <= 0:
        return 0.0
    return max(0.0, min(1.0, value / maximum))


HP_COLOR: RGBA = (0.80, 0.18, 0.20, 0.95)
MP_COLOR: RGBA = (0.20, 0.42, 0.85, 0.95)
EXP_COLOR: RGBA = (0.85, 0.72, 0.25, 0.95)

#: 技能栏最多 5 格（键位 1..5）；顺序由调用方传入（窗口层按学习等级排）
MAX_SKILL_SLOTS = 5


def build_hud(
    *,
    vitals: Vitals,
    progression: Progression,
    wallet: Wallet,
    bag: Bag,
    data: GameData,
    kills: int = 0,
    map_name: str = "",
    width: int = 1280,
    height: int = 720,
    margin: float = 12.0,
    bar_width: float = 220.0,
    bar_height: float = 16.0,
    gap: float = 6.0,
    known_skills: dict[str, int] | None = None,
    skill_names: dict[str, str] | None = None,
    cooldowns: dict[str, float] | None = None,
    shield: tuple[int, int] = (0, 0),
) -> HudLayout:
    """按屏幕尺寸算 HUD 布局（左下角三条状态条 + 右上角统计）。

    布局规则：
    - 左下：HP（最上）→ MP（中）→ EXP（最下，贴底边）；
    - 右上：地图名 / 等级 / 金币 / 击杀 / 背包占用；
    - 所有矩形都夹在 ``[0, width] × [0, height]`` 内。
    """
    width = int(width)
    height = int(height)
    bar_width = min(bar_width, max(40.0, width - 2 * margin))

    def frame_at(index: int) -> Rect:
        y = margin + (2 - index) * (bar_height + gap)
        return Rect(margin, y, bar_width, bar_height)

    def make_bar(index: int, value: float, maximum: float, color: RGBA, caption: str) -> Bar:
        frame = frame_at(index)
        ratio = clamp_ratio(value, maximum)
        return Bar(frame=frame, fill=Rect(frame.x, frame.y, frame.w * ratio, frame.h),
                   ratio=ratio, color=color, caption=caption)

    hp = make_bar(0, vitals.hp, vitals.max_hp, HP_COLOR, f"{vitals.hp}/{vitals.max_hp}")
    mp = make_bar(1, vitals.mp, vitals.max_mp, MP_COLOR, f"{vitals.mp}/{vitals.max_mp}")

    need = data.exp_to_next(progression.level)
    exp = make_bar(2, progression.exp, need, EXP_COLOR,
                   f"EXP {progression.exp}/{need}" if need else "MAX")

    # 右上角统计：x 给右边界，渲染层用 anchor_x="right" 对齐
    texts: list[TextAnchor] = [
        TextAnchor(f"Lv {progression.level}", hp.frame.right + 10, hp.frame.y + 1, 13),
        TextAnchor(f"金币 {wallet.gold}", mp.frame.right + 10, mp.frame.y + 1, 12),
        TextAnchor(map_name, width - margin, height - margin - 14, 13),
        TextAnchor(f"击杀 {kills}", width - margin, height - margin - 32, 12),
        TextAnchor(f"背包 {bag.used}/{bag.capacity}", width - margin, height - margin - 50, 12),
    ]

    # 护盾：只在有盾时出现，避免常驻一行无意义的 0/0
    if shield[1] > 0:
        texts.append(TextAnchor(f"盾 {shield[0]}/{shield[1]}",
                                hp.frame.right + 10, hp.frame.y + 1 + 0, 11,
                                (0.55, 0.85, 1.0, 1.0)))

    panels = {
        "status": Rect(margin - 4, margin - 4, bar_width + 8, 3 * bar_height + 2 * gap + 16),
        "stats": Rect(width - margin - 150, height - margin - 62, 150, 66),
    }

    # 技能栏：屏幕底部居中一排（最多 5 格），键位 1..5 固定。
    # 顺序由调用方决定（窗口层按"学习等级"排好再传进来）——这里只按插入序取前 5，
    # 避免两处各排一遍导致按键与格子错位。
    slots: list[SkillSlot] = []
    if known_skills:
        order = list(known_skills)[:MAX_SKILL_SLOTS]
        slot_w, slot_h, slot_gap = 96.0, 26.0, 6.0
        total = len(order) * slot_w + (len(order) - 1) * slot_gap
        left = max(margin, (width - total) / 2)
        cds = cooldowns or {}
        for index, skill_id in enumerate(order):
            rect = Rect(left + index * (slot_w + slot_gap), margin + 22.0, slot_w, slot_h)
            name = (skill_names or {}).get(skill_id, skill_id)
            slots.append(SkillSlot(
                key=str(index + 1),
                name=name,
                rect=rect,
                ready=cds.get(skill_id, 0.0) <= 0.0,
                proficiency=int(known_skills[skill_id]),
            ))
        panels["skills"] = Rect(left - 4, margin + 18.0, total + 8, slot_h + 8)

    return HudLayout(hp=hp, mp=mp, exp=exp, texts=texts, panels=panels, skills=slots)
