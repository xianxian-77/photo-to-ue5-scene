# NS_Rain 专属雨系统(手搭一次, ~10 分钟, NS_Leaves 同款流程)

> 为什么必须有它(实评定案): NS_Leaves 是为落叶设计的 — **精灵初始朝向随机** + **内置飘移
> 阻力**(落叶 0.9m/s 终速的来源)。参数面救不动: 圆点贴图遮旋转=亮白点="雪", 重力怼到
> -3000 也只是"快一点的雪"。雨的本质 = **速度对齐的半透长条** + **无阻力直落**。
> 管线已就绪: `/Game/Tool/FX/NS_Rain` 存在即自动启用(配竖条水滴贴图/真实重力/冷灰蓝半透),
> 不存在则回退叶系圆滴。

## 步骤(在内容浏览器 /Game/Tool/FX/ 下)

1. **复制** `NS_Leaves` → 重命名 **`NS_Rain`**(右键 Duplicate)。
2. 打开 NS_Rain, 选中发射器, 做 4 处修改:

   **① 精灵渲染器(Sprite Renderer)**
   - `Alignment` → **Velocity Aligned**(速度对齐 — 长条沿下落方向)
   - `Facing Mode` → Face Camera Position

   **② 粒子尺寸改非均匀长条**(Initialize Particle 或 Sprite Size 模块)
   - Sprite Size Mode → **Non-Uniform**
   - X(宽) = `User.Size × 0.12` , Y(长) = `User.Size × 1.6`
     (没有表达式输入就直接给 Dynamic Inputs: Multiply Float, A=User.Size, B=0.12/1.6)

   **③ 删除飘移/阻力**
   - 删掉 **Drag** 模块(或 Drag 设 0)
   - 删掉 Curl Noise / Wind 类摇摆模块(若有; `User.WindXY` 力保留可不删, 管线给雨的风很小)

   **④ 初始旋转归零**
   - Initialize Particle 里 `Sprite Rotation` → 0(删随机)

3. 编译保存。**无需改参数暴露** — 9 个 User 参数原样继承(管线直接驱动)。

## 验收(搭好后跑这两条)
- 重部署雨夜任务 → 眼高截图: 应是**半透灰蓝细长雨丝快速直落**, 非白点非飘移;
- 落地、密度、亮度三项按实际体感终调(管线参数全是活的)。
