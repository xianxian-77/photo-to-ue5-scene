# Niagara 预设手搭说明（3 个通用预设）

> **✅ 已交付(2026-06-11)**:按本文手搭的 `NS_Particles / NS_Leaves / NS_Mesh` 已迁入 `/Game/Tool/FX/`
> 并进仓库 `ue_library/Content/Tool/FX/`(连同 4 个渲染材质,依赖闭合;新工程 `ue_deploy_library.py` 即得)。
> **实搭命名与本文契约的差异(代码侧已适配,系统勿改)**:运行时参数名 = `User.User_<名>`(创建时多打了
> `User_` 前缀);`BoxXY/BoxZ` 合并为 vec3 `User_Box`(x,y,z 半径);`VelZ` 是 vec3 `User_VelZ` 的 z 分量。
> **管线调用入口 = [`ue_fx_presets.py`](../ue_fx_presets.py)**:`spawn_fx(preset, loc, overrides)`,
> 11 个语义预设(fountain/waterfall/rain/snow/dust/embers/leaves/petals/ash/debris/gravel)。
> 验证状态:加载/依赖/CPU sim/参数写入/生成 = 程序化全过;逐预设视觉验收在 PIE 进行(时间性效果静帧测不出)。

> Python 搭不了 Niagara 系统，所以 3 个系统在编辑器手搭；**材质已用代码建好**（渲染器里指过去即可）。
> 按**物理行为**分 3 个通用预设，靠 `User.*` 参数把每个变成具体效果——共性、听 AI 的，不是一效果一系统。

## 0. 通用约定（3 个都遵守）

- **存放路径**：`/Game/AutoImport/Fx/`（没有 `Fx` 文件夹就建一个）。
- **参数名是契约，别改名**——管线按这些**确切名字**驱动。设完参数管线会 `reinitialize_system()` 让其生效（代码里自动做，无需手动）。
- **3 个共享的 9 个基础参数**（每个系统都要有）：

| 参数名 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `User.Color` | LinearColor | (1,1,1,1) | 粒子颜色（精灵乘色 / mesh 染色） |
| `User.SpawnRate` | float | 200 | 每秒生成数（**别留 0**，留 0 不发射） |
| `User.Size` | float | 10 | 精灵大小 cm / mesh 统一缩放 |
| `User.VelZ` | float | 300 | 初速竖直分量（喷溅上抛=正；雨/落水=负；尘/叶≈0~小） |
| `User.VelSpread` | float | 80 | 初速水平随机幅度 |
| `User.GravityZ` | float | -600 | 重力（向下为负；尘=0，叶=-150 轻） |
| `User.BoxXY` | float | 30 | 生成盒水平半径 |
| `User.BoxZ` | float | 10 | 生成盒竖直半径 |
| `User.Lifetime` | float | 1.5 | 粒子寿命（秒） |

> 加 User 参数：左侧 **Parameters** 面板 → **User Exposed** 分组 → 点 **+** → 选类型 → 命名（不带 `User.` 前缀，UE 自动加）。
> 接参数到模块：点模块某个输入值左边的小三角 → **Link Inputs** → 选对应 `User.*` 参数。

---

## 预设 1：`NS_Particles`（软精灵）— 水/雨/雪/尘/火星/雾

**已验证可用。** 一个系统靠参数覆盖所有"软点/液滴"类效果。

### 1.1 建系统
内容浏览器 → `Fx/` → 右键 → **FX → Niagara System** → **New system from selected emitter(s)** → 模板选 **`Templates/Emitters/Fountain`** → 命名 **`NS_Particles`**。

### 1.2 暴露参数：上面**那 9 个**（不多不少）。

### 1.3 接线（发射器堆栈 Emitter）
- **Emitter Update → Spawn Rate**：`Spawn Rate` ← `User.SpawnRate`
- **Particle Spawn → Initialize Particle**：
  - `Color` ← `User.Color`
  - `Sprite Size Mode = Uniform`，`Uniform Sprite Size` ← `User.Size`
  - `Lifetime` ← `User.Lifetime`
- **Particle Spawn → Shape Location**：`Shape Primitive = Box`，`Box Size` = (`User.BoxXY`, `User.BoxXY`, `User.BoxZ`)
- **Particle Spawn → Add Velocity**：`Velocity` = (0, 0, `User.VelZ`)
- **Particle Spawn → 再加一个 Add Velocity in Cone**（或 Add Velocity 用随机范围）：水平随机 ±`User.VelSpread`
- **Particle Update → Gravity Force**：`Gravity` = (0, 0, `User.GravityZ`)
- **Particle Update → Drag**：留默认（喷溅减速更像水）

### 1.4 渲染
- **Render → Sprite Renderer** 保留，`Material` 指到 **`/Game/Auto/M_NiagaraSprite`**（已建好：不受光、乘粒子色、软圆点）。

### 1.5 管线怎么把它变成各效果（仅设参数，无需新系统）
| 效果 | VelZ | GravityZ | Size | Color | VelSpread |
|---|---|---|---|---|---|
| 水·喷溅/喷泉 | +400 | -600 | 中 | 蓝白 | 120 |
| 水·瀑布/落水 | -300 | -800 | 小 | 蓝白 | 40 |
| 雨 | -600 | -1000 | 细 | 灰白 | 20 (BoxXY 大) |
| 雪 | -80 | -150 | 中 | 白 | 60 |
| 尘埃 | ≈0 | 0 | 很小 | 暖白 | 20 (慢飘) |
| 火星/余烬 | +200 | -400 | 小 | 橙(发光) | 80 |

---

## 预设 2：`NS_Leaves`（卡片精灵 + 翻滚 + 飘移 + 可换贴图）— 落叶/花瓣/纸屑/飘灰

叶子不是软圆点：要 ①叶形**贴图** ②**翻滚旋转** ③**自然飘移(风)**。

### 2.1 建系统
同上建一个 Niagara System（Fountain 模板），命名 **`NS_Leaves`**。

### 2.2 暴露参数：**9 个基础 + 下面 3 个**

| 参数名 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `User.RotRate` | float | 90 | 每粒子旋转速度(度/秒)，叶子翻滚 |
| `User.WindXY` | float | 200 | 风/飘移强度（驱动 Curl Noise） |
| `User.Texture` | Texture2D | (留空) | 叶子/花瓣贴图（管线塞 Gemini 生成的图） |

### 2.3 接线（在预设 1 那套接线基础上，再加/改这几处）
- **Particle Spawn → Initialize Particle → Sprite Rotation Mode = Random**（每片起始角随机，别都一样）。
- **Particle Update → 加模块 `Sprite Rotation Rate`**（搜 "rotation rate"）：`Rotation Rate` ← `User.RotRate`（让它持续转）。
- **Particle Update → 加模块 `Curl Noise Force`**：`Noise Strength` ← `User.WindXY`（给叶子自然的旋摆飘移）。`Noise Frequency` 留默认 ~0.2。
- **Particle Update → Drag**：调大默认值（如 2~3，让叶子慢悠悠飘，不像石头砸下）。
- 重力用小值：`User.GravityZ` 管线会给 ~-150（叶子轻）。

### 2.4 渲染（关键：可换贴图）
- **Render → Sprite Renderer**，`Material` 指到 **`/Game/Auto/M_NiagaraLeaf`**（已建好：`贴图×Color` 出色、贴图亮度做 alpha 遮罩、双面不受光——和地面点缀卡片同套路）。
- **把贴图绑到 User 参数**：Sprite Renderer 选中 → 细节面板找 **Bindings / Material Parameters**（或材质里那张图的参数名 `LeafTex`）→ 把它**绑定到 `User.Texture`**。
  - 这样管线 `set_variable_texture("User.Texture", 叶子图)` 就能换图。（这一步若界面里找不到绑定项，退路是改走"管线直接换渲染器材质实例"的方式。）

### 2.5 覆盖的效果（同系统、换贴图+参数）
落叶（秋叶贴图 + 慢飘 + 翻滚）、花瓣（花瓣图 + 更慢）、纸屑/传单（白纸图 + 风大）、飘灰（灰片图 + 微重力）。

---

## 预设 3：`NS_Mesh`（Mesh 渲染）— 3D 碎屑/砂砾/小石子飞溅

有体积的小碎块（不是平片），用 Mesh 渲染器。**网格用一个通用碎屑网格**（不注入任意模型——碎屑是通用的）。

### 3.1 建系统
建 Niagara System（Fountain 模板），命名 **`NS_Mesh`**。

### 3.2 暴露参数：**9 个基础 + 下面 2 个**（此处 `User.Size` = mesh 统一缩放，`User.Color` = mesh 染色）

| 参数名 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `User.RotRate` | float | 180 | 每粒子 3D 翻滚角速度(度/秒) |
| `User.WindXY` | float | 0 | 飘移强度（碎屑通常 0；想要扬尘感再给） |

### 3.3 接线（预设 1 那套基础上）
- **Particle Spawn → Initialize Particle**：
  - `Mesh Scale Mode = Uniform`，`Uniform Mesh Scale` ← `User.Size`（碎屑约 3~10）
  - `Color` ← `User.Color`
  - `Lifetime` ← `User.Lifetime`
  - `Mesh Orientation Mode = Random`（每块朝向随机）
- **Particle Update → 加模块 `Update Mesh Orientation`**（或 `Mesh Rotation Rate`，搜 "mesh rotation"）：旋转速率 ← `User.RotRate`（3D 翻滚）。
- **Particle Update → Gravity Force**：`Gravity` = (0,0,`User.GravityZ`)（碎屑给 ~-900 砸下/落定）。
- **Particle Update → Drag**：默认或略大。
- 想要飞溅：`User.VelZ` 正 + `User.VelSpread` 大（向上四散后落下）。

### 3.4 渲染
- **删掉 Sprite Renderer，加 Render → Mesh Renderer。**
- `Meshes` 数组里放一个**通用碎屑网格**：先用引擎 `/Engine/BasicShapes/Cube`（缩很小就是小石块）顶着；想更像砂砾，放一个低面碎石 mesh（有就放，没有先用 Cube）。
- 材质：指到 **`/Game/Auto/M_NiagaraDebris`**（已建好：默认受光、`Color` 染色的简单材质）；或先用 mesh 自带材质。

### 3.5 覆盖的效果
砂砾/小石子飞溅（VelZ+、重力大、翻滚）、落定的碎块、爆破飞屑、踢起的尘土块。

---

## 管线侧的对接（手搭完成后进行）

1. 先建好 3 个材质（`M_NiagaraSprite` 已有；`M_NiagaraLeaf`、`M_NiagaraDebris` 用桥建）。
2. **逐个验证**：spawn 每个 `NS_*` + 设一组 User 参数 → 看它能发射、受参数控（数量/大小/速度/方向/旋转/换图）。
3. 把特效执行器从"静态卡片"接到这 3 个系统：`analyze_effects` 让 Gemini 决定 **效果类型（soft / leaf / mesh）+ 物理参数 + 颜色 + 贴图** → 管线 spawn 对应 `NS_*` + 设 User 参数 + 定位 → **游戏里会动、任意视角成立**。

**参数名汇总（管线按这些名驱动，务必一致）**：
`Color, SpawnRate, Size, VelZ, VelSpread, GravityZ, BoxXY, BoxZ, Lifetime`（3 个共有）；
`NS_Leaves` 多 `RotRate, WindXY, Texture`；`NS_Mesh` 多 `RotRate, WindXY`。

搭完（或重开 UE 后）跑自检 + 逐个验证 + 接管线。

## 部署机制 v2（2026-06）：帧内计数只当强度档，场景预算 = 面积 × 密度速率

**教训**：把 Gemini 照片帧内可见数量（~180 颗）直接当 3D 场景预算，盒子 6-12m 撒进 ~90m
可玩区 → "数量太少/范围太小/形同虚设"。帧是 2D 取景窗，场景是可游走体积，两者不同量纲。

**分工**（AI 驱动 + 代码保障）：
- **Gemini 决定**：类型（preset）/ 锚点（near_object_id, region）/ 强度档（count：疏<80 / 中<300 / 密）/ 颜色 / 颗粒尺寸。
- **代码保证**：可游走尺度的存在感 —— `NS_DENSITY` 每平米存活速率表 × 可玩区面积 = 场景预算。

**两类部署**（`pipeline_server.py compute_effects`）：
- **覆盖型**（rain/snow/dust/ash）：4 象限发射器铺满全场（单发射器 ≤4000，CPU 安全），
  悬浮型再加 K=3-5 个开阔区**加密囊袋**（3× 局部密度，拒绝采样避开物体脚印）。
- **囊袋型**（petals/leaves/debris/embers）：有锚 → 围绕锚物环带撒 K 袋（树下落孢子、灯旁流萤）；
  无锚 → 开阔区撒。袋盒 8-16m，数量 = 速率 × 袋底面积。
- **公平份额**：`ns_layer_cap = 预算/层数`（≥6000），后续层**收缩不丢层** —— 多样性优先于单层浓度。
  全场存活封顶 `NS_BUDGET_TOTAL=24000`。

**验证**：`dev/_ns_deploy_test.py <task_id>` 离线跑合成计划×真实地形，断言全员存活+界内+预算内。

---

## 附录: 16 预设总表(代码即真源, 此表为速查 — 改 `ue_scene_builder._NS_PRESETS` 时同步)

> 系统: 全部走 `NS_Leaves`(卡片精灵, 可换贴图), 除 gravel 走 `NS_Mesh`。
> `NS_Particles`(加法软精灵) 已退役 — ADDITIVE+UNLIT 在物理照度场景数学性不可见(实测)。
> 亮度: `User_Color = 颜色 × emissive × _fx_lum_scale()`(室外=太阳照度, 室内=indoor_lux);
> fireflies/embers 下限 40(自体发光); mist/smoke/steam 额外 ×6 且 alpha≤0.3(散射环境光)。

| 预设 | 贴图 | SpawnRate | Size | VelZ | Spread | Gravity | Life | Rot | Wind | 部署(室外) | 部署(室内) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rain | streak | 600 | 20 | -900 | 15 | -200 | 1.4 | 0 | 10 | 毯式4象限 | — 禁用 |
| snow | mote | 200 | 8 | -80 | 60 | -150 | 6.0 | 15 | 80 | 毯式4象限 | — 禁用 |
| dust | mote | 60 | 4 | 5 | 20 | 0 | 5.0 | 8 | 60 | 毯+囊袋(视线带1.5-2.5m) | 单发射器满屋毯 |
| ash | mote | 50 | 8 | -10 | 30 | -60 | 9.0 | 60 | 120 | 毯+囊袋 | — 禁用 |
| leaves | leaf | 40 | 22 | -70 | 45 | -60 | 7.0 | 110 | 110 | 囊袋(canopy=冠层3点) | — 禁用 |
| petals | petal | 30 | 14 | -60 | 35 | -50 | 8.0 | 80 | 80 | 囊袋(canopy) | — 禁用 |
| debris | scrap | 40 | 12 | -20 | 50 | -120 | 7.0 | 160 | 260 | 囊袋(地面带) | — 禁用 |
| embers | mote | 80 | 6 | 200 | 80 | -400 | 2.5 | 20 | 40 | 囊袋(around=环带) | 锚定 around 三点环 |
| smoke | fog | 25 | 60 | 75 | 22 | 4 | 6.0 | 6 | 90 | 囊袋(top=顶部点源) | 锚定 top(必须有源) |
| steam | fog | 35 | 38 | 115 | 30 | 0 | 2.4 | 4 | 45 | 囊袋(top) | 锚定 top(必须有源) |
| fireflies | mote | 45 | 6 | 6 | 24 | 0 | 0.9 | 0 | 35 | 囊袋(around=灯旁) | — 禁用 |
| mist | fog | 3 | 320 | 2 | 4 | 0 | 14.0 | 2 | 55 | 5大袋(象限+中心)贴地0.6m | — 禁用 |
| waterfall | streak | 400 | 26 | -300 | 40 | -800 | 2.0 | 0 | 5 | fall_line 底边投影 | — 禁用 |
| fountain | mote | 300 | 12 | +400 | 120 | -600 | 2.0 | 0 | 10 | 锚点单发射器 | — 禁用 |
| gravel | (mesh) | 60 | 4 | 150 | 120 | -900 | 2.5 | 240 | — | 囊袋 | — 禁用 |

- 室内词汇闸门(代码级): 仅 dust/steam/smoke/embers, 其余一律 none(pipeline_server.analyze_effects)。
- 数量: 帧内 count 只是强度档(疏<80/中<300/密), 真实预算 = 面积×NS_DENSITY 档速率,
  全场封顶 24000、单发射器 4000, 层间公平份额(毯78%/囊袋22%)。
- 尺寸钳: dust/ash/embers/fireflies 5-12cm(游戏可见性下限), mist 140-300cm, smoke/steam 25-65cm。
- 已知坑速查: `User_BoxXY`(User_Box 静默无效) / `User_Texture`=MaterialInterface 走
  `set_variable_material` / `get_variable_float` 返回序列取[0] / 同 remote 调用内截图看不到本次生成物。
