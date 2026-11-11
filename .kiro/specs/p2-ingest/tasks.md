# P2 资料引擎：任务

- [x] 1. `ingest/text.py`、`ingest/guard.py` + 测试（R1.1、R1.8）
- [x] 2. `ingest/extract.py` + 测试（R1.2、R1.3）
- [x] 3. `ingest/chapters.py` + 测试（R1.4）
- [x] 4. `knowledge/bm25.py`、`ingest/questions.py`、`ingest/grading.py` + 测试（R1.5、R1.6、R2.2）
- [x] 5. `ingest/figures.py` + 测试（R1.7）
- [x] 6. `knowledge/evidence.py` + 测试（R2）
- [x] 7. `store/`（模型、数据库、任务表）+ 测试（R3、R5.2、R5.3）
- [x] 8. `ingest/parse.py`、`ingest/events.py`、`ingest/pipeline.py` + 测试（R4、R5）
- [x] 9. `cli.py`：ingest、search、courses + `catalog.py` + 测试（R6）
- [x] 10. 样例：`samples/zh-data-structures`、`samples/fetch.py`；golden 快照（由 Skill v5.3 输出生成）与对比测试
- [ ] 11. 验收：覆盖率、ruff、pyright、耗时对比、CI；更新项目记忆

## 验收记录（2026-11-11，本机 Windows，Python 3.12）

| 项 | 结果 |
|---|---|
| Skill 测试迁移 | text、extract、chapters、questions、labels、figures、guard、index、判分全部改写为 pytest 并通过 |
| 中文样例 | 3 章、12 题、10 题有答案；章节、题号、题型、章节归属、答案来源与 Skill 完全一致 |
| MIT 6.006 | 6 章、9 题、9 题有答案；34 张图的页码与图框、10 张题目裁图与 Skill 完全一致（进程池与单进程两种方式） |
| 增量 | 二次导入解析 0 个文件；改一个文件只解析它；删文件后其题目消失、配图删除；放回后重新解析 |
| 任务 | 过期租约回收、未过期租约拒绝并发导入、坏文件最多尝试 3 次 |
| 耗时（CLI 端到端） | 中文样例 0.1s（Skill 0.4s）；MIT 6.006 1.6s（Skill 1.7s） |
| 质量 | 381 个测试通过，总覆盖率 96%，ruff、pyright（strict）零错误 |
