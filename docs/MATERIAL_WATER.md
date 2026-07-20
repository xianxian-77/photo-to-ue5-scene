# MA_YX_Blend 水体使用手册(手册13 课程 × 引擎实测)

> 知识来源: 厂商手册《13.水体细化以及木质结构添加》(桌面 PDF, 渲页存 `output/mat_test/manual13/`)
> + WaterLab 引擎实测扫描(`dev/_water_lab.py` + `dev/_water_sweep.py`, 截图 `output/mat_test/exp/wl*.png`)。
> **代码调用入口: [`ue_water_presets.py`](../ue_water_presets.py)**(预设字典 + `apply_water` + `make_water_lake`)。

## 0. 开水的充要条件(实测,缺一不可)
1. `Use Puddle Layer = ON` —— 整条水外观链(MF_WaterColor→Foam→Moss→Leaves→Rain)的**总闸**;
2. `Use_Water_ImageAlpha = ON` + `water_imagealpha` 遮罩 —— 水的**面积**(白=水: 01缝隙28%/04大面积71%/05全淹);
3. `Water_curve = 0` —— 曲线采样位置(`wateralpha` 曲线递减, 0=全强, **调大=关水**);
4. 平面 4 顶点即可(像素级混合,不需要位移/密网格); 水底 = 同实例的三层混合地面。

## 1. 手册13 课程 → 参数 → 实测笔记
| 手册步骤 | 参数 | 实测(WaterLab 扫描) |
|---|---|---|
| 启用水层 | `Use Puddle Layer` | 总闸(§0) |
| 三层参数调整 | `Use */Layer Adjustments` ×3 | 水底=三层地面,逐层可调色调 |
| 启用水层图片遮罩 | `Use_Water_ImageAlpha` | 面积开关 |
| 替换水遮罩贴图 | `water_imagealpha` | 白=水;遮罩可用暗化系数造任意覆盖率 |
| 遮罩 UV 平铺 | `Water_ImageAlpha Tiling/Offset` | 遮罩图案世界尺寸 |
| 金属度调整 | `Metallic` | **名字骗人**: 非 0-1 金属度(默认 1024!),是水面反射特性旋钮; 0/1024/2048 反射观感可见变化(diff 5.7/7.7) |
| 苔藓混合 | `Moss?` + `Moss_*` | 水面浮藻(实测 22% 像素有效);沼泽味 |
| 水流速度 | `Wave_Speed`/`1/2_GerstnerWave_Speed`/`Ripples_Speed` | 动画参数,静帧测不出,PIE 里调 |
| 水的大小 | `Wave_Size` | 波纹斑块世界尺寸: 512 细碎 / 1024 中 / 2048 开阔(实测图案尺度线性变化) |
| 波纹起伏 | `Ripples_Intensity` (+`1/2_GerstnerWave_Height`) | 微波闪烁 0..4;Gerstner 波幅 0.2 静谧 / 6 风暴(法线图案明显,diff 6.4) |
| 泡沫不透明度 | `Foam?` + `Foam_Opacity` | **默认 1.0 = 全屏爆闪**(放射泡沫动画,时间方差 15+/65%px);**≤0.3 驯服**(0.25 实测 4.65/14%px) → 湖面低开可用,地面水洼禁用 |
| 水的深度 | `Water_Depth` | 水底可见度衰减: 60 见底浅塘 / 260 默认 / 1000 墨黑深水 |
| 水的粗糙度 | `Water_Roughness` | 反射锐度: 0.02 镜面 / 0.1 默认 / 0.3 哑光(影响最大,diff 8.8);**反射好不好看取决于场景有没有天空/反射环境** |
| 水底贴图UV平移 | `Base Layer Tiling/Offset` | 水底纹理对位 |

手册后半(2.岸边细化/4.木质结构/5.交界/6.人物)用的是厂商网格资产(`S_NORDIC_*`、`S_TUNDRA_MOSSY_BOULDER`)——
**不在我们材质包里**;若要,把对应资产包按整合规则进库(正好并入植被清单流程)。

## 2. 预设速查(`WATER_PRESETS`)
| 预设 | 用途 | 关键值 |
|---|---|---|
| `lake_calm` | 平静湖面 | WaveSize 2048 / Ripple 1 / Rough .05 / Depth 400 |
| `lake_windy` | 起风湖+低泡沫 | WaveSize 1024 / Ripple 2.5 / Gerstner 2 / Foam .25 |
| `pond_shallow` | 见底浅塘 | Depth 60 / WaveSize 768 |
| `deep_dark` | 深暗水域 | Depth 1000 / 墨蓝双色 |
| `swamp_mossy` | 沼泽浮藻 | Moss ON / 绿浊色 / Rough .2 |
| `puddles_cracks` | 地面缝隙积水 | mask=T_Alpha_01 / 微波 / Foam 永禁 |

## 3. 调用示例
```python
# UE 编辑器 Python(或 ue_remote_bridge 包一层):
import sys; sys.path.insert(0, r'<path-to-this-repo>')
import ue_water_presets as wp

wp.make_water_lake('Lake01', (0, -60000, 200), size_m=30, preset='deep_dark')   # 一键造湖
wp.apply_water(unreal.load_asset('/Game/Auto/MI_AutoBlendGround'), 'puddles_cracks')  # 地面实例开积水
```
管线侧: `ue_scene_builder._ensure_blend_ground_mic` 的 wetness 水洼即 `puddles_cracks` 同源配方;
FX 湖面(compute_effects 的 water_surface)后续可换 `make_water_lake`(替代半透平面,待办)。

## 4. 坑位清单(再踩自查)
- 水不出来 → §0 四条件逐一对;`Water_curve` 是不是被调大了。
- 爆闪 → `Foam?` 开了且 Opacity>0.3。
- 反射死板 → 场景没天空/反射捕获(材质无辜),见 [[UE_PLUGINS_SETTINGS.md]] 坑 D3(视口曝光)与白屏专项排查笔记。
- 遮罩无效 → 贴图必须 线性+TC_MASKS(管线已自愈,手工导入需注意)。
