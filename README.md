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

## 现状：P2 终局内容已完（攻城 / 赤月牛魔 / 运 9 / 特戒 / 拍卖行 / 佣兵图鉴）

已完成：战斗数学（物 / 魔 / 道三分野）、经验曲线与成长、组权重掉落、
三职业 16 技、等距斜俯视 3D 表现层（角色 / 佣兵 / 图鉴 / 拍卖行 / 商店 / 仓库六个面板）、
沙巴克攻城战役、赤月与牛魔打宝图、幸运 9、特戒六枚、拍卖行、佣兵与图鉴、
ECS 装配（18 个系统：Tick / 回血 / 拍卖 / 佣兵到期 / 冷却 / 状态 / 移动 / 技能 / 追击 /
选敌 / 特戒 / 出刀 / 结算 / 死亡奖励 / 图鉴 / 刷怪 / 攻城 / 传送）。

未完成：药水与续航（当前打宝靠"进图-捞一笔-回城"）、
持久化存档（确定性已就位，读写层未做）。

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
      factory.py        实体工厂（按数据表造人/造怪/造佣兵，怪物与佣兵共用一条构造路径）
      systems.py        14 个 ECS 系统（回血→拍卖→佣兵到期→冷却→状态→移动→追击→
                        选敌→特戒→出刀→结算→死亡奖励→图鉴→刷怪）
      mercenary.py      佣兵规则层（等级门槛 / 名额上限 / 扣钱 / 到期倒计时）
      codex.py          图鉴规则层（击杀记账 / 首杀奖励 / 掉落出处 / 面板数据源）
    skills/
      engine.py         技能系统（学习门槛 / MP / 冷却 / 距离门 / 伤害治疗路由）
    progression/
      exp.py            经验查表 + 连升结转 + 属性成长（含 mc/sc）
    items/
      drops.py          组权重掉落（seed + kill_index 决定，防 save-scum）
      equipment.py      穿脱装备 + 属性重算（裸身基准 + 装备加成，每次全量重算）
      rings.py          特戒六枚规则层（麻痹/护身/复活/传送/隐身/火焰，纯函数 + 记账）
    economy/
      shop.py           商店买卖 + 仓库存取（回收价 1/3，买进卖出必亏）
      auction.py        拍卖行（寄卖行 + 确定性撮合：要价上限 / 概率衰减 / 手续费 / 挂单即离手）
    world/
      spawn.py          按地图刷新表铺怪（开局与换图同一条路径）
      travel.py         传送点判定与换图（PortalSystem，踩上去就过）
    siege/
      campaign.py       沙巴克攻城战役（三波目标/准入锁定/增援/胜负结算，SiegeSystem）
    presentation/
      iso_camera.py     真等距相机（35.264° 俯角 / 正交投影 / 跟随缩放）
      scene.py          实例收集与深度排序（纯数据，无 GL）
      hud.py            HP/MP/EXP 条、状态文本与攻城状态条（纯数据，无 GL）
      panels.py         六个面板的布局与命中测试（角色/佣兵/图鉴/拍卖行/商店/仓库，纯数据无 GL）
      renderer.py       pyglet 2.1 ShaderProgram 渲染（地面网格 1 draw / 实例 1 draw）
      app.py            窗口、输入（WASD/空格/T 特戒/C/M/J/N/B/V 面板/滚轮/鼠标点面板）与固定步长循环
    data/
      loader.py         九张表加载 + 结构校验 + 引用校验
    cli/
      smoke.py          headless 冒烟（10Hz 定步推演）
      play.py           进游戏窗口（--shot 单帧截图，无 GL 时给出可操作提示）
  data/                 九张数值表（手录，1.76 骨架口径）
    exp.json            1-52 级经验曲线（1-50 为国服 1.76 口径，50-52 外推，见文档）
    classes.json        三职业基础属性与成长（含 mc/sc）
    skills.json         技能表（三职业 16 技 + 技能书；P1-S2 起全部 kind 有行为）
    monsters.json       怪物表（23 种，AI 三档：passive/aggressive/elite）
    items.json          装备 22 件（含 P2 赤月/牛魔终局 6 件 + 幸运项链 2 条 + 特戒 6 枚）+ 技能书 16 本
    drops.json          掉落表（组权重 + gold 区间）
    maps.json           地图与刷新/传送表（11 张，等级带 1-52 连续，含战役图 sabak）
    sieges.json         攻城战役表（波次/目标/守军/增援/奖励）
    mercenaries.json    佣兵表（雇用兵/虎卫/魔法侍卫：等级门槛、价格、时长、名额）
  docs/
    数据口径.md          逐表来源、近似标记与已知差异
    借鉴记账.md          借鉴对象 / 许可证 / 范围 / 禁区
  scripts/verify.py     12 条机械门禁（含 --selfcheck 注入破坏自检）
  scripts/extend_p2_luck.py  幂等写幸运数据（终局武器 luck 7 + 两条幸运项链）
  scripts/extend_p2_rings.py 幂等写特戒数据（六枚戒指 + 六只 BOSS 的专属掉落组）
  scripts/extend_p2_auction.py 幂等写寄卖参数（38 件可交易物品的 demand/floor）
  scripts/extend_p2_mercs.py   幂等写佣兵表（三种佣兵 + 等级门槛与名额）
  scripts/inject_check.py  注入验证：把生产代码改坏，确认回归真的会红
  tests/run_tests.py    测试聚合 runner（19 模块）
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

**5. MMO → 单机的转化。** 沙巴克攻城改为"佣兵小队 vs 守军 AI 分层布防"：
三道据点（外城门→内城门→旗台）依次推进，只有当前波次目标可被打
（其余目标 `invulnerable`，打了不结算而不是"砍得动"），守军按波次铺兵 +
定时增援（`max` 封顶），玩家倒下或超时判负。目标 `kind="guard"`
不走死亡奖励，奖励由战役结算统一发。顶部战役状态条常驻显示
波次/目标耐久/守军数/剩余时间。摆摊经济改为商店 + 市场模拟，
抢 BOSS 改为确定性种子世界（掉落熵进存档）。完整转化表见计划书。

**6. 幸运 9 = 把 roll 区间的下限抬到上限。** 传奇的"幸运 9 刀刀上限"在实现上
不是"概率打出上限"，而是 `hi − (hi−lo)×(9−luck)//9` 收窄 roll 区间：luck 0 与裸装
逐位一致，luck 9 塌成单点。这样每刀消耗的随机数个数在 0..8 之间完全一致
（随机流不会因为换装备而错位），代价是两个端点之外的取值仍是线性近似，
标记为 `CALIBRATE`（详见 `docs/数据口径.md` 的《幸运 9》一节）。

**7. 拍卖行是"寄卖行 + 确定性撮合"，四条约束缺一不可。** 单机没有别的玩家，
所以"卖给谁"由系统掷点决定：玩家定价（三档），系统在 `[floor, cap)` 内按
**线性衰减**的概率成交（`cap` = 商店买入价 × 1200‰，挂底价 = 满概率，顶到上限恒为 0）。
四条约束是：要价有上限、越贵越难成交、成交扣 10% 手续费、**挂单即离手**（物品进托管，
不能"挂着还能穿"）。概率曲线刻意选线性——它能被单测逐点钉住，而"更自然的曲线"
只能靠肉眼调。撮合的随机流名是 `auction:{seq}@{round}`，只含挂单序号与第几轮，
所以多挂/少挂一张单不会挪动别的单（详见 `docs/数据口径.md` 的《拍卖行》一节）。

**8. 佣兵复用怪物的一切，图鉴复用死亡事件的一切。** 佣兵的身体（`Attributes`/
`AiState`/`Vitals`）与怪物同构，走 `_build_combatant` 同一条构造路径，只靠
`kind="summon"` + `faction=0` 就让索敌/追击/出手/死亡清理全部生效——不另写一套 AI。
它的三条约束是：花金币、**等级门槛**、**到点必走**（`MercenarySystem` 到期解散，
不留尸体）。图鉴不另起一套记账，而是订阅 `EVT_DEATH` 并复用与死亡奖励**同一个**
`resolve_owner`，所以佣兵打死的怪既进掉落也进图鉴。首次解锁给一笔经验
（怪物经验的 50%，只给一次）——不给属性点，属性点会让战力脱离职业成长表
（详见 `docs/数据口径.md` 的《佣兵与图鉴》一节）。

**9. 特戒的数据形状：效果挂在物品上，接线只有一个入口。** 六枚戒指各自
在 `data/items.json` 里声明 `ring_effect` + `ring_param`（效果名与参数键由 loader
登记，`validate()` 强制齐全），代码里不写"如果是麻痹戒指就……"这种分支表。
六条效果按**触发时机**分三类：挨打类（护身/复活）挂在
`CombatResolver._damage`——那是全项目唯一允许改血的地方，而且顺序被钉死为
「魔法盾 → 护身 → 复活判定 → `take()`」；出手类（麻痹/火焰/破隐）挂在 `_apply`
之后；主动类（传送）走 `T` 键。三者的随机流都带 `ring:` 前缀，戴上或摘下戒指
不会挪动主伤害流的位置（详见 `docs/数据口径.md` 的《特戒六枚》一节）。

## 测试与验收

```bat
python -m tests.run_tests              :: 19 个模块，269 项无窗口单测
python -m mirsolo.cli.smoke --ticks 3000   :: headless 冒烟（含确定性复跑）
python -m mirsolo.cli.play --level 50 --gear --siege sabak --shot shots\siege.png
                                       :: 攻城验收：开窗即开战，顶部战役状态条
python scripts\verify.py               :: 12 条机械门禁（<1 秒）
python scripts\verify.py --list        :: 只看门禁清单
python scripts\verify.py --verbose     :: 每条门禁的证据
python scripts\verify.py --selfcheck   :: 注入破坏，证明门禁拦得住
python scripts\inject_check.py         :: 42 处注入，全部要能把测试打红
```

门禁清单：

| 门禁 | 断言 |
|------|------|
| `data-integrity` | 掉落/刷新/技能职业/攻城引用全部可解析，无死链 |
| `data-files` | 九张表齐全且是合法 JSON |
| `map-bands` | 等级带连续 + 传送网有向全连通（战役图除外，但必须声明 `base_map`） |
| `tests-registered` | `run_tests.MODULES` ↔ `tests/test_*.py` **双向**一致 |
| `readme-commands` | README 里每条 `python -m` 目标都真实可导入 |
| `readme-tree-real` | README 项目结构里的路径都真实存在 |
| `luck-sources` | 幸运只来自武器/项链、值域合规、且**三个职业各自**都凑得到 9 |
| `ring-effects` | 六枚特戒六效齐备、参数值域合规、每枚都有掉落来源 |
| `auction-book` | 38 件可交易物品都能寄卖，且**底价高于商店回收价**（否则拍卖行白做） |
| `merc-hire` | 佣兵门槛练得到、价格随门槛递增、全局上限**真的够得着**（不许是死约束） |
| `codex-coverage` | 图鉴覆盖的每一种怪都有掉落来源（不许出现"无掉落"行） |
| `package-imports` | `mirsolo/` 下每个模块都能 import |

## 路线图

| 批次 | 内容 | 状态 |
|------|------|------|
| P0 | 战士垂直切片：比奇省外围 + 矿洞、6 怪、8 装、练到 20 级 | 逻辑核 ✅ / 表现层 ✅ |
| P1 | 三职业全技能、地图带（蜈蚣洞→祖玛）、沃玛/祖玛 BOSS、商店仓库、技能熟练度 | ✅ |
| P2 | 沙巴克攻城战役、赤月/牛魔、特戒与运 9、拍卖行、佣兵与图鉴 | ✅ 全部完成 |

## 许可

MIT，见 [LICENSE](LICENSE)。
