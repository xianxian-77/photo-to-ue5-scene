# 室内房间壳(Indoor Shell)立项设计 — 2026-06

## 背景与定位

管线的室外分支已完整:地形(照片深度反投影)→ 远景环(AI 方位形态)→ HDRI 天穹(AI 视觉选图)
→ 中景剪影 → 雾带。室内分支目前只有"地板 + 物体 + 检测灯具"的极简形态 —— 本立项给室内
配齐与室外同完成度的"世界容器":**房间壳**。

架构原则不变:**AI 判定一切参数,代码只做几何与物理执行**;`is_indoor` 闸门两分支互不污染。

## 一、AI 判定层(analyze_room,新)

对室内照片输出(全部由 Gemini 从透视线索/天花板高度/材质推断):

```json
{
  "room_size_m": [w, d, h],          // 房间盒尺寸(由灭点/家具尺度推断)
  "camera_in_room": [x_frac, y_frac],// 相机在房间平面的位置(0-1)
  "wall_material": "plaster|wallpaper|brick|concrete|wood|tile",
  "wall_color": [r,g,b],
  "floor_material": "wood|tile|carpet|concrete|marble",
  "ceiling_type": "flat|beams|vaulted",
  "openings": [                       // 窗/门(墙面 + 横向位置 + 尺寸)
    {"wall": "front|back|left|right", "kind": "window|door|arch",
     "u_frac": 0-1, "w_m": x, "h_m": x, "sill_m": x}
  ],
  "outside_hint": "day garden|city street|night sky|...",   // 窗外世界一句话
  "indoor_lux": <50-2000>             // 室内整体照度(物理曝光复用现有公式)
}
```

## 二、执行层(ue_scene_builder,新 _apply_room_shell)

1. **房间盒网格**:程序化生成内表面朝内的盒体(墙/地/天花板分槽位),开口处布尔留洞
   (顶点级留洞,同地形网格的 MeshDescription 路径,免布尔库)。
2. **照片匹配材质**:复用地形纹理管线(`generate_terrain_textures` 换提示词:
   "wall plaster swatch matched to this photo" 等)→ 墙/地/顶三张 + `_make_tileable`。
3. **窗外世界**:开口外贴 HDRI 板(复用 Poly Haven 管线按 `outside_hint` 选图)+
   微视差(板后退 2-4m);门=暗廊盒。
4. **室内光**:已有链路直接通 —— 灯具检测(analyze_lights)+ AI 实用光(practicals)
   + 窗口日光(开口处一盏面积光,方向/色温由 env);物理曝光公式照用(lux 域 50-2000)。
5. **粒子子集**:已通用 —— 光柱微尘(dust 锚定窗口)、蒸汽(厨房)、烟(壁炉 attach=top);
   毯/囊袋部署自动退化为锚点模式(无地形路径已存在)。
6. **评审闭环**:照常(布局/曝光/补光处方/FX 调参全复用,室内无需新机制)。

## 三、不做的事(防过度工程)

- 不做多房间/户型推断(单房间盒够覆盖单照片重建);
- 不做真窗外可行走世界(HDRI 板即可,VR 视差在窗框距离下可接受);
- 房间盒不追求毫米精度(物体仍按既有投影摆放,壳只是"容器")。

## 四、阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | analyze_room + 盒体网格(无开口)+ 三面材质 | 室内照片→有墙有顶,曝光正常 |
| P1 | 开口留洞 + 窗外 HDRI 板 + 窗口面积光 | 窗有景、光有向 |
| P2 | 粒子/实用光/评审在室内场景回归 | 闭环全绿 |

**触发条件**:第一张室内照片进入管线时启动 P0(或手动触发)。
