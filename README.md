# mirsolo — 热血传奇单机复刻

> **致敬复刻，非官方项目。** 本项目不含任何原版素材（贴图 / 音效 / 地图 / 文本），
> 不使用「热血传奇」「Legend of Mir」等商标作为产品或发行名，
> 不复用任何 GPL 代码。玩法机制与数值为公开资料自行整理的近似模型，
> 详见 [docs/数据口径.md](docs/数据口径.md) 与 [docs/借鉴记账.md](docs/借鉴记账.md)。

把《热血传奇》（Legend of Mir 2, 1999）那套 MMORPG 内容，做成**一个人也能通关的单机 ARPG**：
三职业 / 比奇省打磨 / 地牢打宝 / 沃玛祖玛赤月 / 沙巴克攻城——但对手全是 AI。

- **技术底座**：`3dworld`（渲染与确定性工具链）+ `allin-engine`（ECS / 事件总线 / 固定步长）
  + `dse-core`（字典场景）+ `bloodmusic`（程序化音频）
- **零外部素材**：所有贴图 / 音效 / 模型都由代码生成
- **确定性**：纯整数随机流，同种子同结局；掉落与击杀序号绑定，读档重打不刷装
- **零 GL 可测**：逻辑层无窗口单测，headless 冒烟 3 秒跑完 2000 秒游戏时间

## 现状：P0 垂直切片（逻辑核已可跑）

已完成：战斗数学（物 / 魔 / 道三分野）、经验曲线与成长、组权重掉落、ECS 装配
（Tick / 回血 / 冷却 / 选敌 / 攻击 / 死亡奖励 / 刷怪 共 7 系统）、headless 冒烟。

未完成：等距斜俯视 3D 表现层、三职业全技能、背包界面、存档 UI、BOSS AI 分层。

## 快速开始

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e .

:: headless 冒烟：2000 秒游戏时间，看打怪-掉装-升级闭环
python -m mirsolo.cli.smoke --ticks 20000

:: 机器可读摘要（CI / 门禁用）
python -m mirsolo.cli.smoke --ticks 20000 --json
```

## 项目结构

```
mirsolo/
  mirsolo/
    determinism.py      确定性随机流（splitmix64 + 流隔离，纯整数）
    components.py       ECS 组件（六维属性 / 生命 / 背包 / 成长 …）
    bootstrap.py        GameEngine 装配：引擎 + 系统 + 世界数据键
    combat/
      formulas.py       纯函数公式（命中 / 物理 / 魔法 / 道术 / 治疗）
      resolver.py       结算器（唯一允许改血的地方，发事件）
    actor/
      factory.py        实体工厂（按数据表造人/造怪）
      systems.py        10 个 ECS 系统（回血→冷却→选敌→移动→技能→追击→出刀→结算→奖励→刷怪）
    skills/
      engine.py         技能系统（学习门槛 / MP / 冷却 / 距离门 / 伤害治疗路由）
    progression/
      exp.py            经验查表 + 连升结转 + 属性成长（含 mc/sc）
    items/
      drops.py          组权重掉落（seed + kill_index 决定，防 save-scum）
      equipment.py      穿脱装备 + 属性重算（裸身基准 + 装备加成，每次全量重算）
    economy/
      shop.py           商店买卖 + 仓库存取（回收价 1/3，买进卖出必亏）
    world/
      spawn.py          按地图刷新表铺怪（开局与换图同一条路径）
      travel.py         传送点判定与换图（PortalSystem，踩上去就过）
    presentation/
      iso_camera.py     真等距相机（35.264° 俯角 / 正交投影 / 跟随缩放）
      scene.py          实例收集与深度排序（纯数据，无 GL）
      hud.py            HP/MP/EXP 条与状态文本（纯数据，无 GL）
      panels.py         角色/商店/仓库面板布局与命中测试（纯数据，无 GL）
      renderer.py       pyglet 2.1 ShaderProgram 渲染（地面网格 1 draw / 实例 1 draw）
      app.py            窗口、输入（WASD/空格/滚轮/鼠标点面板）与固定步长循环
    data/
      loader.py         七张表加载 + 结构校验 + 引用校验
    cli/
      smoke.py          headless 冒烟（10Hz 定步推演）
      play.py           进游戏窗口（--shot 单帧截图，无 GL 时给出可操作提示）
  data/                 七张数值表（手录，1.76 骨架口径）
    exp.json            1-50 级经验曲线
    classes.json        三职业基础属性与成长（含 mc/sc）
    skills.json         技能表（三职业 16 技 + 技能书；P1-S2 起全部 kind 有行为）
    monsters.json       怪物表（17 种，AI 三档：passive/aggressive/elite）
    items.json          装备 8 件 + 技能书 16 本（book 的 teaches 双向校验）
    drops.json          掉落表（组权重 + gold 区间）
    maps.json           地图与刷新/传送表（8 张，等级带 1-50 连续）
  docs/
    数据口径.md          逐表来源、近似标记与已知差异
    借鉴记账.md          借鉴对象 / 许可证 / 范围 / 禁区
  scripts/verify.py     7 条机械门禁（含 --selfcheck 注入破坏自检）
  scripts/inject_check.py  注入验证：把生产代码改坏，确认回归真的会红
  tests/run_tests.py    测试聚合 runner（13 模块）
```

## 设计要点

**1. 公式与施加分离。** `combat/formulas.py` 是纯函数（不建世界、不写文件），
`combat/resolver.py` 是唯一改血的地方。单测因此可以只测公式，
端到端测试只测"血掉没掉"，两边不互相将就。

**2. 每次 roll 都记账。** 结算结果带 `rolls`，"这刀为什么只打 3 点"
能还原到具体随机数：命中 roll / 攻 roll / 防 roll 逐项可见。

**3. 位置无关的随机。** 随机流名包含 `(seed, 实体 id 对, tick)`，
所以结算顺序不影响结果——回放可对任意单帧复算，不依赖"从头重跑"。

**4. 数据驱动，代码只认 ID。** 全部数值在 `data/*.json`；
引用的物品/怪物/职业/传送目标由 `GameData.cross_check()` 校验，
`scripts/verify.py` 把它做成门禁。

**5. MMO → 单机的转化。** 沙巴克攻城改为"佣兵小队 vs 守军 AI 分层布防"，
摆摊经济改为商店 + 市场模拟，抢 BOSS 改为确定性种子世界（掉落熵进存档）。
完整转化表见计划书；P0 只落"打怪-掉装-升级"这条最小闭环。

## 测试与验收

```bat
python -m tests.run_tests              :: 13 个模块，155 项无窗口单测
python -m mirsolo.cli.smoke --ticks 3000   :: headless 冒烟（含确定性复跑）
python scripts\verify.py               :: 6 条机械门禁（<1 秒）
python scripts\verify.py --list        :: 只看门禁清单
python scripts\verify.py --verbose     :: 每条门禁的证据
python scripts\verify.py --selfcheck   :: 注入破坏，证明门禁拦得住
```

门禁清单：

| 门禁 | 断言 |
|------|------|
| `data-integrity` | 掉落/刷新/技能职业引用全部可解析，无死链 |
| `data-files` | 七张表齐全且是合法 JSON |
| `tests-registered` | `run_tests.MODULES` ↔ `tests/test_*.py` **双向**一致 |
| `readme-commands` | README 里每条 `python -m` 目标都真实可导入 |
| `readme-tree-real` | README 项目结构里的路径都真实存在 |
| `package-imports` | `mirsolo/` 下每个模块都能 import |

## 路线图

| 批次 | 内容 | 状态 |
|------|------|------|
| P0 | 战士垂直切片：比奇省外围 + 矿洞、6 怪、8 装、练到 20 级 | 逻辑核 ✅ / 表现层 ⬜ |
| P1 | 三职业全技能、地图带（蜈蚣洞→祖玛）、沃玛/祖玛 BOSS、商店仓库、技能熟练度 | ⬜ |
| P2 | 沙巴克攻城战役、赤月/牛魔、特戒与运 9、拍卖行、佣兵与图鉴 | ⬜ |

## 许可

MIT，见 [LICENSE](LICENSE)。
