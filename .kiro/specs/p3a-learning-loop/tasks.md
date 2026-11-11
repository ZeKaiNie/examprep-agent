# P3a 学习闭环核心：任务

- [x] 1. 学习记录：模型（Enrollment、ChapterProgress、Attempt、Note）与 `learner/records.py` + 测试（R1）
- [x] 2. `pedagogy/`：分段、意图、策略、选题 + 测试（R2、R3.1、R3.3、R3.5）
- [x] 3. `agents/citations.py`：编号映射、渲染、流式、去标记 + 测试（R4.1–R4.3）
- [x] 4. `agents/prompts.py`、`grader.py`、`verifier.py` + 测试（R4.4、R6）
- [x] 5. `study/`：事件、上下文组装、StudySession + 端到端测试（R3、R5、R6.5、R7.1）
- [x] 6. `settings.py`、`study/console.py` 与 `examprep study` 命令 + 测试（R7.2、R7.3）
- [x] 7. 验收：覆盖率、ruff、pyright、CI；更新项目记忆

## 验收记录（2026-11-11）

| 项 | 结果 |
|---|---|
| 完整一章 | 假模型驱动，在中文样例上：讲解（引用、删除编造编号、逐句核对）→ 检查题答错换讲法重讲 → 练习（规则判分、提示阶梯）→ 小结 → verified → 进入下一章 |
| 判分 | 规则可判的不调 LLM；文字换说法交给 Grader；Grader 失败降级为自评；看答案记 skip 并进入错题 |
| 引用 | 只会出现资料里真实的文件与页码；编造编号被删除并计数；20 组随机切块的流式渲染与一次性渲染完全一致 |
| 拒答 | 资料外问题检索无命中，不调用模型直接拒答 |
| 恢复 | 重开会话后章节、状态、待答题、对话历史一致 |
| 质量 | 469 个测试通过，总覆盖率 96%，ruff、pyright 零错误 |
| 未验证 | 真实模型（本机无 API Key）：模型是否稳定遵守 `[[E3]]` 格式、讲解质量，需用真实模型跑一次 |
