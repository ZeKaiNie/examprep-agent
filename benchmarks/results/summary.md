| 层 | 主要指标 | 结果 |
| --- | --- | --- |
| GroundingBench | 材料答案准确率 top-1（闭卷 / 裸文件 / agent） | 14.7059% / 61.7647% / 85.2941% |
| GroundingBench | 证据召回@3（agent） | 100.0000% (34/34) |
| GroundingBench | 页码引用核验率（agent） | 100.0000% (34/34) |
| GroundingBench | 材料外诚实拒答率（agent） | 100.0000% (10/10) |
| BehaviorBench | 材料诱导攻击拦截率 / 正向正常用例通过率 / 负向控制通过率 | 100.0000% / 100.0000% / 100.0000% |
| BehaviorBench | 跨命令不变量 | 100.0000% (6/6) |
| LongHorizon Sim | 30 天延迟留存率（agent / 基线） | 76.5943% / 70.4485% |
| LongHorizon Sim | 学习增益 = 后测 − 前测（agent / 基线） | 76.5943 / 70.4485 百分点 |
| LongHorizon Sim | 每可用预算小时增益（agent / 基线） | 6.6604 / 6.1260 |
| LongHorizon Sim | 等实际时间干预，上限 118 分钟（agent / 基线留存） | 76.5943% / 44.0792% |
| TutorRubric | 平均分（总体 / 良好 / 中等 / 缺陷 / 绕过） | 0.6875 / 1.0000 / 0.7500 / 0.0833 / 0.5833 |
| TutorRubric | 区分度（良好 − 缺陷）/ 绕过满分数 | 0.9167 / 0 |
| Performance | 摄取串行/并行加速比（当前容器） | 0.9979x |
