# 粮仓通风控制中枢

在仓房传感器、风道设备与安全限制基础资料之上的 Python 通风控制中枢。
温度使用摄氏度，相对湿度使用百分比，压力使用帕，所有时间均携带明确时区。

夜间降温能否开机，不能只看仓外温度。系统在给出建议时综合四类信号并**逐条留存依据**：

- **粮堆露点**：送风露点 vs 冷端（可信测点最低粮温）的结露裕度；
- **降温收益**：外温需比冷端粮温低出最小有效温差；
- **风道压力**：静压越限或采样缺失即禁止该风道启动；
- **分时电价**：峰段只建议推迟（`DEFER_TO_OFF_PEAK`），谷段才计入成本最优。

## 安全语义（不可被普通控制覆盖）

| 情形 | 系统行为 |
| --- | --- |
| 熏蒸限制时段 | `FORBIDDEN`，任何角色批准/强制均被拦截 |
| 降雨且外湿超阈（雨雪倒灌风险） | `FORBIDDEN`；仅小雨低湿时只记录观察依据 |
| 风道静压异常 / 压力采样缺失 | 对应风道禁止启动 |
| 送风露点逼近粮温（裕度不足） | 禁止启动；运行中出现则立即建议 `STOP` |
| 同风道相邻风机互锁 | 每组最多运行 `max_running_per_duct_group` 台，第二台拿不到启动计划 |
| 某层传感器漂移/失效 | **隔离该点**，风险深度区间按相邻电缆间距外扩，无相邻电缆一侧一直包络到粮堆边界；聚合只取可信测点 |
| 紧急人工停机 | 任何在岗值班员可一键 `FORCE_STOP_ALL`，不等待优化周期、不需要批准 |
| 进程崩溃后重启 | 凡没有“已停机确认回执”的风机一律 `UNVERIFIED` 待核实，**绝不假定为安全停机**，须经理现场核实 |

控制闭环为：**建议（带依据）→ 有权限值班员确认 → 指令 → 设备回执**。
建议有有效期（默认 300 秒），批准下发前用最新数据复检，批准后条件转坏会拦截启动或改走停机。
硬门禁永远不可被 `FORCE_OVERRIDE` 绕过；强制覆盖仅对峰段推迟、降温收益不足等软建议开放。

## 模块结构

```
grain_aeration/
├── models.py         领域模型与事件（建议/批准/指令/回执）
├── psychrometrics.py 露点（Magnus 公式）与结露裕度
├── tariff.py         分时电价与跨时段电费积分
├── config.py         reference/domain.json 加载与安全阈值
├── sensor_health.py  漂移隔离与风险区间扩大
├── advisor.py        带依据的控制建议
├── controller.py     批准闭环、复检、互锁兜底、紧急停机、会话结算
├── persistence.py    JSONL 事件日志与崩溃恢复（唯一事实来源）
├── hardware.py       设备网关协议与内存模拟实现
└── audit.py          时间线回放与实际/预期对标
```

事件以 JSONL 追加落盘（`fsync`）。崩溃恢复重放事件流：
只有 `confirmed=true, running=false` 的设备回执才能证明风机关闭，
启动指令后缺停机证据、停机指令未被确认，都恢复为待核实。

## 使用

```bash
python3 -m unittest discover -s tests   # 30 个安全语义测试
python3 examples/night_run.py           # 投诉夜复盘 + 完整闭环演示
```

最小调用：

```python
from grain_aeration import (
    AerationController, ApprovalDecision, JsonlEventStore,
    SensorQuality, SensorReading, User, WeatherSnapshot, load_domain,
)
from grain_aeration.hardware import SimulatedFanGateway
from grain_aeration.models import DuctPressure
from datetime import datetime

domain = load_domain("reference/domain.json")
clock = lambda: datetime(2026, 9, 12, 23, 0).astimezone()
at = clock()
gw = SimulatedFanGateway([f.id for f in domain.fans], clock=clock)
ctrl = AerationController(domain, JsonlEventStore("events.jsonl"), gw, clock=clock)

rec = ctrl.advise(
    readings=[SensorReading("T-TOP-1", 0.5, at, 24.8, 70, SensorQuality.GOOD)],
    weather=WeatherSnapshot(at, 17.0, 65, rain=False),
    pressures=[DuctPressure("DUCT-N", at, 350.0, ok=True)],
)
print(rec.action, [(e.code, e.blocks_start) for e in rec.reasons])

manager = User("zhao", "quality_manager")
ctrl.approve(rec, manager, ApprovalDecision.APPROVE)   # 值班员无批准权
ctrl.emergency_stop(User("sun", "operator"), reason="巡检异常")  # 任何人可急停
```

每次控制结束后：

```python
summary = ctrl.close_session(rec.rec_id, cold_end_before_c=24.8, cold_end_after_c=22.3)
# 实际能耗按“启动确认→停机确认”区间在分时电价上积分，而非额定估算

from grain_aeration.audit import benchmark, build_timeline, format_timeline
print(format_timeline(build_timeline(ctrl.store, rec.rec_id)))
for row in benchmark(ctrl.store):   # 预期 vs 实际温降/能耗/电费，标出偏离
    ...
```

## 资料引用关系

`reference/domain.json` 将测点绑定到粮层深度，记录风机互锁组、
同夜外界气象片段与熏蒸限制；时间区间满足 `starts_at < ends_at`，
引用一致性由 `tests/test_reference.py` 校验。
