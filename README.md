# 粮仓通风领域资料

本仓库维护仓房传感器、风道设备和通风限制的基础数据。温度使用摄氏度，相对湿度使用百分比，压力使用帕，所有时间均携带明确时区。

`reference/domain.json` 将测点绑定到粮层深度，并记录风机的互锁组。`outside_weather` 是同一夜间的外界变化片段，`restrictions` 表示不可由普通控制覆盖的安全限制。

可执行以下命令检查资料的引用关系：

```bash
python -m unittest discover -s tests
```
