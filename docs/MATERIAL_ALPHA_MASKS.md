# Alpha 遮罩研究 —— `T_Alpha_01..05` 对层间混合比例的影响（预设参考）

> 非论文部分，纯工程参考：方便按"想要的混合效果"直接调取遮罩预设。
> **本版结论全部来自 UE 引擎内真渲染**（2026-06-11，截图在 `output/mat_test/exp/`，驱动脚本 `dev/_exp_driver.py`）。
> 早先 numpy 模拟版的"越白→上层"极性是**错的**，已按引擎实测修正 —— 教训：必须引擎渲染验证，像素模拟会骗人。

## 0. imagealpha 在材质里干什么（实测语义）
`MA_YX_Blend` 三个遮罩参数节点（厂商手册 p6-7 + 引擎实测）：
- `base_imagealpha`（第1↔第2层）：**白 = 露 Base（第1层），黑 = 露 Middle（第2层）**
- `top_imagealpha`（第2↔第3层）：**白 = 露 Top（第3层）**，且链路里 `top_curve` 影响大，顶层极易泛滥（见 §2）
- `water_imagealpha`（地面↔水）：**白 = 水**。⚠️ 必须同时开 **`Use Puddle Layer`**（水外观总开关）+ `Use_Water_ImageAlpha`（面积遮罩），缺一不出水（见 §4）

记法：**遮罩的白色露出"遮罩名字对应的那层"**（base→Base、top→Top、water→水，三者统一）。
对应开关 `Use_Base/Top_ImageAlpha`（静态开关，改了要重编译几秒）。
遮罩贴图要求：**线性 (sRGB=off) + TC_MASKS**（04/05 原导成 sRGB 已修正）；遮罩存成 RGBA 且 alpha 恒 1，但材质采样的是 RGB 灰度（R 通道），实测工作正常。

⚠️ 历史坑（已修，根因是母材质 3 个 imagealpha 节点**默认贴图丢失**）：节点贴图为 NULL 时，开 `Use_*_ImageAlpha` → SM6 编译失败 → 整材质丢失。两个母材质（原版 `MA_YX_Blend` 由我修复并同步回仓库 `ue_library/`；`matellic/MA_YX_Blend_me` 是手修副本）现均无 NULL 节点。**管线统一用原版**（仓库自包含的那份）。

## 1. 五张遮罩 = 一条"Base↔Middle"配比光谱（引擎实测，E1）
设置：Base=泥土、Middle=草、`Use_Base_ImageAlpha=ON`，逐张换 `base_imagealpha`（`e1_mask01..05.png`）：

| 遮罩 | 平均亮度* | 引擎里的效果（白=露Base/土） | 预设用途 |
|---|---|---|---|
| **T_Alpha_03** | 0.115 | **几乎全草**，零星土点 | "基本纯 Middle"，最克制的底层点缀 |
| **T_Alpha_01** | 0.283 | 草为主，**土在裂纹/划痕处成斑**（细节最丰富，与04并列） | 风化感、磨损、缝隙露底 |
| **T_Alpha_02** | 0.307 | 草为主，土斑**边缘最柔**（硬边最少 frac_hard≈0.007） | 大面积自然软过渡（推荐默认） |
| **T_Alpha_04** | 0.714 | **土为主**，草挤在暗缝里（≈01 反相） | 底层主导、上层透缝 |
| **T_Alpha_05** | 0.882 | **几乎全土**，仅细暗丝留草 | "基本纯 Base" |

\* 像素统计（3 次独立重算一致，最大差 0.0004）；亮度占比 ≈ 引擎里 Base 层的面积占比（Base 过渡近似线性，`base_curve` 默认=1）。

## 2. 顶层（Top）过渡:`top_curve` 影响大，岩石极易泛滥（E3）
Base=土、Middle=草、Top=岩，`Use_Top_ImageAlpha=ON`，实测（`e3_*.png`）：
- `top_imagealpha=T_Alpha_05/04/02` → **99~100% 全岩**；只有最暗的 `T_Alpha_03` 才剩 36% 草（64% 岩）。
- **`top_curve` 调小反而更泛滥**（=1 或 0.5 → 100% 岩）。⚠️ 注意：`base/top/Water_curve` 是 **CurveAtlasRowParameter（曲线采样位置 t）**，不是普通标量——曲线资产在 `CoreContent/curves/`（`topalpha`/`wateralpha` 递减 1→0，`basealpha` 递增 0→1）。top 链的精确代数未完全解出（t=1 与 4 采样值相同但渲染不同），**按实测点用**：想要"岩石点缀"= **最暗遮罩 03 + top_curve=4（默认）**。
- `Top Layer Blend Controls` 的 G 在 imagealpha 模式下**无效**（与 Base 组接线不同）。
- **覆盖率连续旋钮 = 遮罩暗化**（5 张库存遮罩之外的自由度）：把遮罩在 numpy 里乘系数再导入，即可精确控制顶层覆盖率。实测（`t1/t2/q1/q2`）：`03 原版(0.115)→64% 岩`、`03×0.6(0.069)→中量岩块`、`03×0.4(0.046)→2.5% 点缀`、`03×0.2→近零`。已生成 `T_Alpha_03_x60/x40/x20` 进库（`/Game/Tool/Masks/`，已同步 `ue_library/`）。这是 Gemini"覆盖率百分比→遮罩"的通用机制。

## 3. Blend Controls 矢量4分量（官方名 + 实测效果，E2）
手册 p3：**R=Blend Amount，G=Blend Contrast，B=Blend Falloff，A=Invert Blend**（默认 1,1,1,0）。
imagealpha 模式实测（Base/Middle 过渡、遮罩=T_Alpha_02、土占比变化）：
- **G（Contrast）= 主旋钮**：G=0→7.6%，G=1→10.4%，G=2→14.7%（近似把遮罩对比拉开 → 白区扩张）
- **B（Falloff）弱效**（9.3%→10.7%，主要影响边缘）
- **R（Amount）/ A（Invert）完全无效**（4档像素级一致）——应属于高度混合通路，imagealpha 下不参与
- ⚠️ A 不是想当然的"反相开关"（实测无效），想反相直接换互补遮罩（01↔04、03↔05）

## 4. 高度混合 = 赢者通吃；水/叠加层 = 双开关机制（E3-E7 + 图追线 + w1-w6，已破解）
- **`Use_*_ImageAlpha` 全关（高度混合）→ 整面只剩一层**（位移贴图全平时无梯度可混，画面还发暗）——这就是当初"只有一个草"的机制根因。位移没接真高度图前**别用高度混合**。
- **水/叠加层机制（图追线 `dev/_trace_water3.py` 定案）**：整条水外观链 `MF_WaterColor →(Refraction?)→(Foam?)→(Moss?)→(Leaves?)→(Rain?)→ … → Use Puddle Layer → BlendMaterialAttributes`——**`Use Puddle Layer` 是水分支的总闸**，OFF 时整条链被静态切除；`Use_Water_ImageAlpha`+遮罩只是给最终混合提供**面积 alpha**。所以：
  - **出水配方 = `Use Puddle Layer` ON + `Use_Water_ImageAlpha` ON + 遮罩（白=水）+ `Water_curve=0`**。实测：遮罩05→整面水（`w3`）、遮罩01→**水积在裂缝洼地**（`w4`，效果极好）。
  - `Foam?`/`Refraction?` = 水面细节；`Moss?` = 水上藻（`w5`，22% 像素变化）；`Rain?` 静帧变化微弱（动态涟漪，待动图验证）；`Leaves?` 同链未单测。**这些都活在水分支里，Puddle OFF 时全部无效**——E5 当时"全员零贡献"的真相。
  - ⚠️ **`Foam?` 是"爆闪"元凶**（视口目视实证 + 时间方差实测）：RadialFoam 是放射扩散的白色泡沫动画，开着时 6 秒内 64.6% 像素变化、全屏亮度脉冲 74.6→89.6；关掉后像素级零抖动。**地面水洼一律 `Foam?=OFF`**（它是配雨圈/岸线用的）。
  - ⚠️ **遮罩相关性陷阱**：`base` 和 `water` 用同一张遮罩时，水正好积在土的区域上把土全盖掉（`t1` 实测土不可见）。**不同用途的遮罩要错开**（如土=02、水=01、岩=03_x60），四要素即可同屏（`q2_decorr_x60.png`）。
  - 🍂 **水面落叶/苔藓 = 顶点色驱动**（`dev/_trace_leaves.py` 定案：两个 `MF_WaterMoss` 调用都有 `VertexColor` 输入引脚）：`Leaves?` ON 还不够，**顶点色要刷白**（代码：`unreal.MeshVertexPainterLibrary.paint_vertices_single_color(comp, LinearColor(1,1,1,1), True)`，5.8 类名带 Library）→ 水面铺满噪声卷曲的落叶（`l5_leaves_vtxwhite.png`）。图案由 `Leaves_Scale/NoiseScale/NoiseIntensity` 控制。实测苔藓在黑顶点色下也显、落叶必须白 → 两调用吃的顶点色通道/极性不同（按经验用：刷白全都显）。管线接法：按 biome 程序化刷顶点色控制落叶/苔藓覆盖区。
  - **UV 渐变家族无关地面**：`use_base/top_Ugredient`、`*_ImageAlpha_UVgredient` 开关、`Base/Top/Water_uv_gredient`（曲线位置）实测在方形平铺地面上 ≤0.4% 像素变化——它们是给**条带/样条网格**（河岸横向渐变）用的方向性混合，我们的地面用 imagealpha 即可，**忽略这组**。
  - **反射归因**：水面"反射丑/死板"= **场景没有可反射的天空/反射捕获是旧黑图**（测试关卡实测：0.03 粗糙度镜面对照板都全哑 → 渲染环境级问题，非材质）。正式管线场景有 `_apply_reflection`+天空球，不在此列；若仍不满意，材质侧旋钮：`Water_Roughness`(默认0.1，0.15-0.25 更稳)、`Wave_Intensity`(0.3，调低更平静)、`Ripples_Intensity`(2)、`FakeRefraction_Intensity`(1)。⚠️ `Metallic` 默认 1024——名字骗人，不是 0-1 金属度，别当金属度调。
  - `Water_curve` 是曲线采样位置：`wateralpha` 曲线**递减**（t=0→输出1.0=水全强，t≥1→0=关）。**默认 0 就是全开**；我曾设 2.0 想"加强"，实际是把水乘零（自坑实录，引以为戒）。
- 已排除项（过程记录）：NULL 编译失败（已修）、顶点色（白/黑无差）、母材质差异、网格密度（4 顶点平面就能出水——水是像素级混合，不需要位移）。

## 5. 管线/Gemini 对接的预设映射（实测背书）
ue_scene_builder 实例化时（统一 parent=仓库原版 `MA_YX_Blend`，`duplicate_asset` 工作模板再覆盖）：
- "草地、缝里露土 / 风化" → `base_imagealpha=T_Alpha_01`，Use_Base_ImageAlpha=ON
- "两种地表自然渐变" → `T_Alpha_02`（要更多 Base：`Base/Middler Blend Controls` G 1→2）
- "基本纯草偶有杂色" → `T_Alpha_03`；"土为主草透缝" → `T_Alpha_04`；"基本纯土" → `T_Alpha_05`
- "岩石点缀" → `top_imagealpha=T_Alpha_03` + `top_curve=4`（默认）；岩石主导 → 02/04/05 任意（都会泛滥）
- "积水/雨后水洼" → `Use Puddle Layer=ON` + `Use_Water_ImageAlpha=ON` + `water_imagealpha=T_Alpha_01`(缝里积水)/`04`(大面积水)/`05`(全淹) + `Water_curve=0` + `Water_Color_01/02` 按水色 + `Refraction?=ON`；`Moss?` 出藻；**`Foam?` 永远 OFF（爆闪）**
- **验证过的"全要素"基准配方**（`q2_decorr_x60.png`：草+土斑+岩块+水洼）：Base=土 `02` / Middle=草 / Top=岩 `03_x60`（要更少→`03_x40` 点缀级）/ 水 `01` / `Water_curve=0` / `top_curve=4` / Puddle+WaterImageAlpha+Refraction ON / Foam OFF
- 每个贴图槽必须填、遮罩线性+TC_MASKS、静态开关改完等几秒重编译（黑帧重试）

## 6. 全自动实验方法（复用）
`dev/_exp_driver.py <verify|e1|e2|e3|e4|e5>`：桥上设参 → `take_high_res_screenshot(1280,720,name,Blend3Cam)` → **Win32 唤起 UE 窗口**（后台节流时不渲帧；`Slate.bAllowThrottling 0`/`t.IdleWhenNotForeground 0` 不够，必须唤起）→ 本地轮询 `Saved/Screenshots/WindowsEditor/` → 拷回 `output/mat_test/exp/` → 黑/白帧自动重试（防 shader 编译竞态）。
分析：`dev/_montage.py`（拼图）、`dev/_crop_compare.py`/`dev/_e3_metrics.py`（草/岩/土面积占比 = 绿优势/低饱和分类）。
