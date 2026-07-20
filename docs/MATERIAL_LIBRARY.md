# 外部材质库（整合进代码库 + 一键部署到任意工程）

> 目标：把下载的专业材质整合进代码库，让 `git clone` 后别人无需再下载素材、管线代码自动把它装进任意 UE 工程。
> 方式：**content-copy**——材质内容树提交进仓库 `ue_library/Content/`，`ue_deploy_library.py` 把它拷进每个工程的 `Content/`，资产挂 `/Game/Tool/...`。

## 0. 为什么是 content-copy（不是插件）
这批 `.uasset` 的内部包路径被烤死成 `/Game/Tool/...`（实测：`MA_YX_Blend` 引用 `/Game/Tool/MA_Blend/CoreContent/...`）。所以必须落在工程的 `Content/Tool/` 下才能加载、引用才不断。做成 `/AutoLibrary/` 插件要把 201 个资产逐个改路径（有断引用风险），content-copy 零修改、最稳。

## 1. 路径与内容
- **仓库内（提交进 git）**：`ue_library/Content/Tool/MA_Blend/` —— `MA_YX_Blend` 母材质（3 层 PBR 混合 + 完整 WATER）+ `CoreContent/`（函数/曲线/贴图）。~223MB，单文件最大 35MB。
- **仓库外（dev-only，不进 git）**：`Desktop/ue_library/_source/`（原始下载 + 3 变体 + 遮罩），只用于重新打包。

## 2. GitHub 约束（已核对）
- 单文件最大 35MB < 100MB 硬限 → 不需要 Git LFS。
- ~223MB < 1GB 软建议 → 直接提交，别人 clone 零额外工具就拿到。

## 3. 部署到任意工程（代码侧，已就绪）
```powershell
python ue_deploy_library.py [<工程目录>]     # 缺省 H:\5.7TEST\TEST5_71
```
- 纯文件操作、不需要编辑器：把 `ue_library/Content/*` 拷进 `<工程>/Content/*`，资产挂 `/Game/Tool/...`。幂等（已在工程里就跳过）。
- 跑完重启编辑器（或 rescan `/Game`），`MA_YX_Blend` 即出现在 `/Game/Tool/MA_Blend/`。
- 已单元测试：拷贝 / 幂等跳过 / 缺内容报错。

## 4. 一次性打包（如何生成 ue_library/Content，需 UE 一次）
1. 把 `_source` 的原始 `.uasset` 拖进某工程的 Content Browser 导入（别只磁盘拷，会丢依赖）；3 变体共用的 CoreContent 只导一份；遮罩导入成贴图。
2. 资产保持在 `/Game/Tool/...`（包路径如此，不改）。
3. 把 `<该工程>\Content\Tool\` 拷进仓库 `ue_library\Content\Tool\` → `git add` 提交。从此仓库自包含。

## 5. 管线接入（已接通 v1 —— 地形路径）
`ue_scene_builder.py` 的 `_ensure_blend_ground_mic(texes, env, til_u, til_v)`（`_apply_terrain` 里优先调用）：
- **输入全部来自既有管线**：3 张照片匹配无缝贴图（`pipeline_server.py generate_terrain_textures` → `terrain.albedo_urls`）进 Base/Middle/Top 三层；`env.ground_wetness/roughness`（Gemini）驱动水洼与粗糙度。
- **参数=引擎实测基准**（docs/MATERIAL_ALPHA_MASKS.md）：遮罩去相关（土 `02`、顶层 `03_x60`、水按湿度 `03/01/04` 三档）、`base/top/Water_curve=1/4/0`、`Foam?` 永远 OFF（爆闪）、wetness≥0.35 才开"双开关"水洼（`Use Puddle Layer`+`Use_Water_ImageAlpha`）。
- **实例来源**：复制厂商工作模板（槽全填、绝不 NULL 编译失败）→ 挂回仓库原版母材质 → 存盘 `/Game/Auto/MI_AutoBlendGround`。
- **回退**：库未部署（没跑 `ue_deploy_library.py`）或任一步失败 → 自动退回程序化 `M_AutoTerrainVar`，移植不崩。
- 已端到端验证（`output/mat_test/exp/int1_dry.png`=干三层混合、`int2_wet.png`=湿出积水）。
- **待接（下一步）**：平场路径（`_apply_ground`/`_apply_ground_skirt` 是世界对齐平铺，需世界对齐变体）；Gemini 按 biome 选遮罩/覆盖率系数（暗化遮罩=连续覆盖率旋钮）；FX 水面换 Gerstner。

## 6. 注意（性能 / 工程设置）
- 这套带 Tessellation/Displacement，高帧率下开销很大；性能敏感时优先用不带位移的设置。
- RVT 变体需 Project Settings ▸ Rendering ▸ Enable virtual texture support（当前只 bundle 了 `MA_Blend` 基础变体，未含 RVT）。
- 许可按素材来源；论文 demo 用途没问题，注明出处即可。

> 通用约定：所有外部预设/资源都整合进代码库（提交进 git），保证上 GitHub 自包含、别人 clone 即用。
