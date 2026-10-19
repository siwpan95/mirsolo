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
    anchor_x: str = ""
    """``"left"``/``"right"``；留空表示由渲染层按位置自动判断（HUD 右上角统计用）。"""


@dataclass(frozen=True)
class SkillSlot:
    """技能栏一格：按键号 + 名称 + 是否就绪。"""

    key: str
    name: str
    rect: Rect
    ready: bool
    proficiency: int = 1


@dataclass(frozen=True)
class SiegeBanner:
    """攻城状态条：顶部居中一横条，装"打哪、还有多少、剩多久"。

    原版攻城靠行会频道喊话同步进度，单机没有队友，所以进度必须常驻可见——
    否则玩家砍完城门不知道该往哪走。

    ``objective`` 是**当前波次目标**的血条（旗台/城门耐久），
    ``wave_text`` 是"第 2/3 波 · 内城"，``guards``/``clock`` 是辅助信息。
    """

    frame: Rect
    objective: Bar
    title: str
    wave_text: str
    guards_text: str
    clock_text: str
    urgent: bool = False
    """剩余时间不到 1/5 时置真，渲染层把时钟染红。"""


@dataclass
class HudLayout:
    hp: Bar
    mp: Bar
    exp: Bar
    texts: list[TextAnchor] = field(default_factory=list)
    panels: dict[str, Rect] = field(default_factory=dict)
    skills: list[SkillSlot] = field(default_factory=list)
    """已学技能栏（最多 5 格，按技能 id 稳定排序，与学习顺序无关）。"""
    siege: SiegeBanner | None = None
    """战役状态条；不在攻城时 ``None``（不占屏、不画空框）。"""


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

#: 战役状态条的几何（顶部居中）
SIEGE_BANNER_WIDTH = 420.0
SIEGE_BANNER_HEIGHT = 54.0
SIEGE_BANNER_TOP = 18.0
SIEGE_BAR_HEIGHT = 12.0
#: 剩余时间低于总量这个比例就标红（1/5）
SIEGE_URGENT_RATIO = 0.2


def build_siege_banner(status: dict, *, width: int, height: int,
                       margin: float = 12.0) -> SiegeBanner | None:
    """把 :func:`mirsolo.siege.campaign.siege_status` 的快照算成顶部状态条。

    ``status`` 为 ``None`` 或已结束时返回 ``None``——打赢/打输之后不该还挂着
    一条"第 3/3 波 旗台 0/2600"的条子。

    宽度会被钳到屏幕内（窄窗口下不横向溢出），这是唯一的几何约束。
    """
    if not status or status.get("finished"):
        return None
    width = int(width)
    height = int(height)
    banner_w = min(SIEGE_BANNER_WIDTH, max(120.0, width - 2 * margin))
    x = (width - banner_w) / 2
    y = height - SIEGE_BANNER_TOP - SIEGE_BANNER_HEIGHT
    frame = Rect(x, y, banner_w, SIEGE_BANNER_HEIGHT)

    hp = int(status.get("objective_hp", 0))
    hp_max = int(status.get("objective_max", 0))
    ratio = clamp_ratio(hp, hp_max)
    bar_frame = Rect(frame.x + 10, frame.y + 8, banner_w - 20, SIEGE_BAR_HEIGHT)
    bar = Bar(frame=bar_frame,
              fill=Rect(bar_frame.x, bar_frame.y, bar_frame.w * ratio, bar_frame.h),
              ratio=ratio, color=HP_COLOR, caption=f"{hp}/{hp_max}")

    wave = int(status.get("wave", 0)) + 1
    count = int(status.get("wave_count", 0))
    wave_text = f"第 {wave}/{count} 波 · {status.get('wave_name', '')}"
    title = f"{status.get('name', '')} · {status.get('objective_name', '')}"
    guards_text = f"守军 {int(status.get('guards_alive', 0))}"

    elapsed = int(status.get("elapsed", 0))
    limit = int(status.get("limit", 0))
    if limit > 0:
        left = max(0, limit - elapsed)
        # 逻辑 10Hz：把刻换算成 mm:ss，玩家读的是时间不是 tick
        clock_text = f"剩余 {left // 600}:{(left % 600) // 10:02d}"
        urgent = left <= limit * SIEGE_URGENT_RATIO
    else:
        clock_text = ""
        urgent = False

    return SiegeBanner(frame=frame, objective=bar, title=title, wave_text=wave_text,
                       guards_text=guards_text, clock_text=clock_text, urgent=urgent)


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
    siege: dict | None = None,
    mining: str = "",
) -> HudLayout:
    """按屏幕尺寸算 HUD 布局（左下角三条状态条 + 右上角统计）。

    布局规则：
    - 左下：HP（最上）→ MP（中）→ EXP（最下，贴底边）；
    - 右上：地图名 / 等级 / 金币 / 击杀 / 背包占用；
    - 左上区（状态条右侧）：等级 / 金币 / 护盾；
    - 右上：地图名 / 击杀 / 背包；
    - 底部居中：技能栏；
    - 顶部居中：攻城状态条（仅战役进行中）。
    - 左下角状态条正上方：挖矿一行（**只在挥锄时出现**，由调用方决定内容）。
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

    # 挖矿：调用方传进来的是已经拼好的一句话，空串就不占这一行。
    # 刻意不做成常驻的“未开工”——站在城里也挂一行是噪音
    if mining:
        texts.append(TextAnchor(
            mining, margin, margin + 3 * (bar_height + gap) + 12, 12,
            (1.0, 0.86, 0.48, 1.0)))

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

    return HudLayout(hp=hp, mp=mp, exp=exp, texts=texts, panels=panels, skills=slots,
                     siege=build_siege_banner(siege, width=width, height=height,
                                              margin=margin))
