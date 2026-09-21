# 粮仓机械通风控制中枢（WH-07）

本仓库在粮仓传感器/风道基础资料之上，提供一套 Python 通风控制中枢：
**系统先产出带依据的控制建议，由有权限值班员确认后才下发指令**；硬性安全条件
（熏蒸、雨雪倒灌、风道压力异常、相邻风机互锁、急停）可以直接否决任何启动。

温度为摄氏度、相对湿度为百分数、压力为帕、功率为千瓦，所有时间均携带时区
（`Asia/Shanghai`）。仅使用 Python 3.11+ 标准库。

## 事故背景

一次夜间降温中，值班员只看到仓外干球温度下降便启动风机。但同一时刻存在多重相反信号：

- 仓房处于 **18:00–06:00 磷化氢熏蒸期**（硬禁止）；
- 外界 **19.2°C / 91%RH 且正在降雨**，露点约 17.7°C，已高于粮堆最冷点 15.8°C
  （干球在降、露点却在逼近——只看干球必然误判）；
- 风道静压 **-1480 Pa** 超出 [-1200, -100] Pa 参考区间，疑似倒灌后风阻异常；
- 时段为平段电价，并非谷段。

运行演示可看到系统如何把这些信号汇总为一条 `DEFER` 建议：

```bash
python3 -m scenarios.night_incident
```

## 目录结构

| 路径 | 职责 |
| --- | --- |
| `reference/domain.json` | 仓房、测温电缆与测点（粮层/深度/坐标）、风道与风机互锁、压力采样、气象片段、分时电价、安全限制 |
| `grain_aeration/psychrometrics.py` | Magnus 公式露点；入风露点 vs 粮堆最冷点的结露判据（含安全裕量） |
| `grain_aeration/tariff.py` | 峰/平/谷分时电价与预期能耗电费 |
| `grain_aeration/sensors.py` | 漂移/失效测点**隔离**，并沿电缆与同层**扩大风险区间**（裕量 +1°C） |
| `grain_aeration/safety.py` | 不可覆盖的安全否决门 |
| `grain_aeration/advisor.py` | 生成 `START / DEFER / STOP` 建议与全部证据、预期指标 |
| `grain_aeration/fans.py` | 风机状态机、设备网关（ACK/NAK）、**重启后待核实**语义 |
| `grain_aeration/controller.py` | 中枢门面：建议→批准→执行前二次校验→回执→急停→复盘→恢复 |
| `grain_aeration/store.py` | 仅追加 JSONL 事件审计日志（追加即 fsync） |
| `grain_aeration/timeline.py` | 建议→批准→指令→回执时间线回放与恢复摘要 |
| `grain_aeration/data.py` | 资料文件加载 |
| `scenarios/night_incident.py` | 事故复盘与安全窗口全流程演示 |

## 核心规则

1. **建议与执行分离**：`tick()` 只产出建议；`approve()` 需值班员/质量经理/管理员；
   `execute()` 前用**当前**现场快照重跑安全门（批准后可能开始下雨或进入熏蒸窗口）。
2. **结露判据看露点而非干球**：入风露点必须 ≤ 粮堆最冷温度 − 安全裕量（默认 2°C，
   扩大风险区间内 +1°C）。
3. **传感器漂移**：该点立即隔离、不参与任何计算；同缆上下点与同层最近点进入扩大
   风险区间；隔离需管理员登记原因并写入审计。
4. **硬性否决**：熏蒸/密闭/检修、降雨或倒灌窗口、气象缺失、风道压力越界或缺测、
   同互锁组已有非停机风机（含待核实/故障）、急停锁定。
5. **相邻风机互锁**：F-1/F-2 同属 `DUCT-N`，建议每批每组至多一台；运行中出现
   结露/降雨/熏蒸等风险时，建议直接转 `STOP`。
6. **紧急人工停机不等周期**：`emergency_stop()` 立即对全部非停机风机下发停机并锁定
   启动，复位需质量经理/管理员且风机均已证实安全。
7. **重启恢复保守化**：从事件日志重放；凡拿不出"ACK 且 running=False"回执的被操作
   风机一律进入 `PENDING_VERIFICATION`，**绝不默认安全停机**，须现场人工核实。
8. **复盘**：每轮记录实际降温、实际 kWh/电费与预期偏差及 kWh/°C，供质量经理比对。

## 快速使用

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from grain_aeration.controller import AerationController
from grain_aeration.data import *
from grain_aeration.fans import SimulatedGateway
from grain_aeration.models import Actor, Role
from grain_aeration.store import EventStore

TZ = ZoneInfo("Asia/Shanghai")
data = load_domain("reference/domain.json")
ctrl = AerationController(
    sensors=parse_sensors(data),
    fans=parse_fans(data),
    restrictions=parse_restrictions(data),
    gateway=SimulatedGateway(),
    store=EventStore("audit.jsonl"),
)
at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
rec = ctrl.tick(at, latest_readings(data, at),
                parse_weather(data)[-1], parse_pressures(data, at)).recommendation

operator = Actor("U-102", "张伟", Role.OPERATOR)
ctrl.approve(rec.id, operator, at)          # 值班员确认
ctrl.execute(rec.id, operator, at)          # 下发并收取设备回执
print(ctrl.timeline(rec.id))                # 回放完整时间线
```

生产部署时将 `SimulatedGateway` 替换为 PLC/Modbus 实现（继承 `DeviceGateway`），
事件日志路径指向持久化目录即可获得重启恢复能力。

## 测试

```bash
python3 -m unittest discover -s tests
```
