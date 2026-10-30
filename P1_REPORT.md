# P1 报告：安全层（P0.5）+ 学习者模型

日期：2026-10-30
基线：P0 commit `c0262c6`（140 tests）
验收：`python -m pytest -q` → **267 passed, 1 skipped**（跳过项：本机未安装 PDF 后端）
红线：未修改 `research/skill-repo/`；未 commit、未 push。

本报告如实记录已实现内容、借鉴来源、未做项与 P2 建议。P1 开工时已有 `security/`、
`memory/`、`learning/graph.py`、`learning/planner.py` 的主体，但 5 个测试失败且验收冒烟未覆盖；
本次完成的是缺陷修复、交付补齐与验收测试，而非从零重写。

---

## 1. 交付对照（P1_BRIEF → 实现 → 测试）

### A. 安全层（PLAN §5.7）

| 任务书要求 | 实现 | 测试 |
| --- | --- | --- |
| A1 权限闸门 L0–L3，模型不可绕过/自提权 | `security/risk.py`（`RiskLevel`/`Access`/`Decision`）；`ToolRegistry.register` 强制 `risk=`，风险是注册属性；`ToolRegistry.execute` 先过 `PermissionGate.check` 再调 handler；模型传入的 `risk/force/authorized` 等保留键被 gate 忽略并在调用 handler 前剥离；授权根移到用户配置目录，工作区控制文件（`permissions.json`/`.security/`/`audit/`/`.snapshots/`/`memory/`/`study_state.json` 等）对模型文件工具只读 | `test_security.py::TestRiskLevelAndGate`、`TestControlFilesAreModelReadOnly`、`TestThrottleAndReservedArguments::test_reserved_model_arguments_are_stripped_before_the_handler` |
| A2 路径围栏：材料只读、默认工作区、越界需 L2 授权、可撤销、CLI 管理 | `security/paths.py`（realpath + `normcase`/NFC 比较防 `..`/软链/大小写逃逸）、`security/filetools.py`（handler 内二次校验）、`security/permissions.py`（授权存用户配置目录、按目录/能力授权、`PermissionStore.revoke`）；工具注册显式声明 `path_arguments`，gate 另递归识别常见路径键；CLI `examprep permissions list/grant/revoke` 拒绝盘根/home/home 祖先/UNC 根 | `TestPathFence`、`TestPathArguments`、`TestPermissionsStore`、`TestPathNormalisation`；`test_p1_acceptance.py::test_permissions_cli_grant_list_revoke`、`test_permissions_grant_refuses_over_broad_targets` |
| A3 删除语义：`.trash/` + 保留期，不做真删；状态文件原子写+快照 | `security/trash.py`（移动 + `retain_until` + 可 restore；根/祖先保护与 restore fence）；`security/atomicio.py`（mkstemp → fsync → `os.replace`，写前快照，旧快照移入 `.expired`）；`tools/state.py`、`tools/figures.py`、`setup` 重建均改走回收站 | `TestTrash`、`TestGrantTargetsAndTrashFence`；`test_session.py` 的 checkpoint/原子写用例；`examprep_agent/` 内 grep 无 `os.remove/unlink/rmtree` |
| A4 命令硬拦截（any exec 工具 + 材料命令样式文本） | `security/commands.py` 确定性扫描器，做 shell-ish 归一化（去引号、拼接续行、展开 `$HOME`/`%USERPROFILE%`、去重定向）并解码 base64 管道；内核 gate 对 execute/L3 参数扫描；`setup` 扫描全部材料与题库并把 findings 写入 state + 审计 `material_scan` | `TestCommandScanner`（含引号/变量/选项顺序/重定向/续行/Windows 变体/Base64、多行代码块）；`test_adversarial.py::test_setup_records_every_scanner_attack_family` |
| A5 审计与熔断：allow/deny/grant 事件、密钥脱敏、写入阈值熔断 | `security/audit.py`（JSONL、`redact_secrets`、`read_jsonl` 容错、`limit=0` 语义）；gate 默认创建审计，allow 决策记录解析后的 `paths`，grant/revoke 写事件；`WriteThrottle` 滑动窗口持久化，超限返回 `write_throttle` 并拒绝；registry+handler 只计一次（I6）。熔断范围限定为“模型可触发的工具写”，主机侧原子写明确记录为受信操作 | `TestAudit`、`TestAuditCoverage`、`TestThrottleAndReservedArguments`、`test_write_throttle_opens_after_threshold_and_persists` |
| A6 对抗集 100% 拦截（诱导删文件/越界写/泄露 key/题库注入） | 见下文 §3；四类攻击材料在 `setup` 被记录，模型据其发起的四类动作被 gate 分别拒绝且无副作用；另有改名/嵌套路径、控制文件删除等变体 | `tests/test_adversarial.py`（6 个用例，含生产 explain loop 端到端与 library-level gate 测试；生产 CLI 写入工具接线见 §5 延期登记） |

### B. 学习者模型

| 任务书要求 | 实现 | 测试 |
| --- | --- | --- |
| B1 知识图谱：节点/先修边/来源/用户修订/落盘 | `learning/graph.py`：章节节点（含 sources/concepts）、顺序边、仅向后引用的材料引用边（`第N章`/`Chapter N`/`§N`，避免环）、`graph_edits` 重放、`knowledge_graph.json`；CLI `graph --add-prereq/--remove-prereq/--rebuild` | `test_graph_planner.py::TestKnowledgeGraph`（建图、前向引用忽略、修订+环拒绝、阻塞链、损坏回退）；`test_p1_acceptance.py::test_graph_cli_revisions_are_stored_and_reload` |
| B2 掌握度传播：BKT 按 KP；next/goto 先修阻塞并说明原因；status 图谱视角 | `learning/learner.py` BKT 四参数；`learning/planner.py::unmet_prerequisites` 走 `blocking_chain`（含传递先修）；`recommend` 返回 `prereq_blocked`+原因参数；`cmd_next`/`cmd_goto` 双语提示；`status` 输出已解锁/阻塞/待复习 | `test_blocking_chain_is_deepest_first_and_transitive`、`TestPlanner::test_unmet_prerequisite_redirects_to_the_prerequisite`；`test_p1_acceptance.py::test_next_redirects_to_the_unmet_prerequisite_with_a_reason`、`test_status_exposes_graph_review_and_retention_views` |
| B3 FSRS 接入教学循环：due 队列为 next/plan 第一优先；每日新学上限；突击排序；status 到期数/预测留存 | `planner.due_reviews/recommend/review_plan`；`cmd_next` 按 `review_first`→prereq→daily_limit→new；`cmd_plan` 首行 due；`CramPolicy` 排序分 = 真题频率 × 未掌握 × 章节级时间压力（剩余天数低于该章练习债务所需天数时提升，因此考试日期会改变章节相对顺序）；`scheduler.predicted_retention`；`status` 输出到期数与预测留存 | `test_due_reviews_outrank_prerequisites_and_new_material`、`test_daily_new_limit_stops_new_chapters`、`test_cram_orders_by_urgency_frequency_and_unmastered`、`test_cram_order_changes_with_the_exam_runway`、`test_review_first_policy_controls_the_next_action`、`test_due_review_retrievability_is_reported`；`test_p1_acceptance.py::test_plan_lists_a_manually_due_card_first` |
| B4 长期记忆三层结构化落盘、可恢复、损坏降级 | `memory/episodic.py`（JSONL 事件流，state history + agent 日志幂等导入；坏行隔离到 content-addressed `.corrupt-*` sidecar）、`semantic.py`/`procedural.py`（完整 schema 校验、类型损坏 quarantine 后从 `.snapshots` 恢复、再退回空默认）、`Session` 作答时写三层且记忆异常不穿透主流程；同分钟作答按 `(qid, ts, result)` 去重 | `tests/test_memory.py`（三层落盘+重载、错误聚类、结构损坏降级、快照恢复、导入幂等/同分钟去重、agent 日志归一） |
| B5 借鉴纪律 | FSRS-4.5 公开公式独立实现并注明 `open-spaced-repetition/fsrs4anki` 参考；记忆分层概念注明受 Letta/MemGPT（Apache-2.0）启发、未拷贝代码；未引入 py-fsrs 或其他运行时依赖（仍仅 `pydantic`）。详见 §2 | `test_learning.py::TestFSRSReferenceVectors`；`pyproject.toml` 依赖仅 pydantic |

### 验收冒烟

`tests/test_p1_acceptance.py` 在样例课程上真实走 CLI：

1. `setup → answer q006 → 手工把该卡 due 改为 2020-01-01 → plan`：`plan` 第一项（早于“第 1 天”）即该复习卡 `q006`；
2. `把 ch1 掌握度清零、current=2 → next`：输出“先修阻塞 … ch1 掌握度 0% < 阈值 80%”并给出 `examprep goto 1`；`goto 2` 也给出同样警告；
3. `status` 同时输出图谱/复习/每日新学视图。

---

## 2. 借鉴来源（如实）

- **FSRS-4.5**：公式与默认权重来自公开的 FSRS-4.5 规范/`open-spaced-repetition/fsrs4anki`（见 `learning/scheduler.py` 文件头与权重注释）。**没有引入 py-fsrs**，也没有拷贝其代码；`tests/test_learning.py` 用公开参考向量（首评/次评 S、D、interval）钉住系数，防止“单调但错误”的实现蒙混。
- **Letta / MemGPT**：只借鉴“episodic → semantic → procedural”的记忆分层思想（`memory/__init__.py` 已注明 Apache-2.0 来源与“未拷贝代码、不同存储模型”）；实现是工作区内纯 JSON/JSONL，无向量库。
- **universal-examprep-skill**（MIT, © ZeKaiNie）：P0 工具层与 guard 的既有来源，各移植文件头保留声明；P1 的注入/凭据外泄检测是在其 guard 基础上扩展，未改 `research/skill-repo/`。
- 运行时依赖：仍只有 `pydantic`（PDF 可选 `pypdfium2`，dev 可选 `pytest`）。未新增 py-fsrs 等可选依赖。

---

## 3. 本次实际修复（P1 半成品 → 全绿）

修复 5 个失败测试，均为实现缺陷而非放宽断言：

1. `graph._concepts` 只认 `##`–`######`，漏掉材料/章节常见的 `#` 一级标题 → 改为 `#{1,6}`。
2. 三个 planner 测试直接以 `session.learner.states["chN"]` 设置掌握度，但 BKT 模型只在答题时懒创建状态 → 新增 `KnowledgeStates`：直接索引时按 `p_init` materialize，`mastery()` 仍不创建，保持 `setup --fresh` 后 `kps == {}` 的既有语义（未改 P0 测试）。
3. `windows_del_root` 只拦盘根，漏掉 `del /f /s /q C:\Users\student` → 扩展到用户/系统目录树。
4. 对抗测试暴露的命令扫描器两个真实缺陷：
   - 正则只作用于整段文本的 `^`/`$`，多行材料/代码块中的命令漏检 → 全部命令模式加 `re.MULTILINE`，并允许内嵌于句中使用；
   - 英文常见语序 “send the API key to URL” 未被覆盖 → 新增 `send_secret_to_url` 模式；
   - gate 把命令参数 `json.dumps` 后再扫描，尾部引号破坏了 `$` 锚点，导致 `run_command` 落到 `risk_l3` 而非 `dangerous_command` → 字符串参数按原文扫描（`permissions.py` 与 `loop.py` 两处）。

新增测试：`tests/test_adversarial.py`（毒材料四类攻击）、`tests/test_p1_acceptance.py`（任务书冒烟 + permissions/graph CLI 全链路），并给 `test_security.py` 补了多行/内嵌命令与良性文本回归参数。通过数 186 → 204（收集数 192 → 205，初始另有 5 failed；仍 1 skipped）。

### 3.1 `REVIEW_P1.md` 阻断/重要项修复记录（本轮）

审阅基线为 204 passed；修复后 **267 passed, 1 skipped**。每项均补了以审阅复现片段为输入的回归测试。

| 编号 | 修复 | 回归测试 |
| --- | --- | --- |
| **B1** 控制文件在 L1 可写区 | 授权根移到 `$EXAMPREP_CONFIG_DIR`（默认 `~/.config/examprep/permissions/<workspace-hash>.json`），工作区 `permissions.json` 只保留为证据且被忽略；gate/path policy 将 `permissions.json`、`.security/`、`audit/`、`.snapshots/`、`.trash/`、`memory/`、`logs/`、`study_state.json`、`session.json`、`knowledge_graph.json`、`chapters.json`、`quiz_bank.json`、`figures.json`、`index.json` 设为模型写/删只读（`internal_path_read_only`） | `test_security.py::TestControlFilesAreModelReadOnly`（自授权、擦审计、重置熔断、删 state、读仍允许）、`test_legacy_workspace_permissions_file_is_ignored` |
| **B2** 路径参数白名单/无 gate fail-open | `ToolRegistry.register(..., path_arguments=...)` 显式声明；gate 递归遍历嵌套 dict/list 并识别 `*_path`/`*_file`/`*_dir`/`paths` 等键；无 gate 的 registry 对一切非 read 访问（含 L1 write）返回 `gate_required`，L2/L3 保留原有拒绝码；注册时强制 `execute/network ⇒ risk ≥ L2` | `test_security.py::TestPathArguments`（`output_path`/`file_path`/`options.path`/`paths`/`PATH`/声明键/内容字符串不误伤/裸 registry）、`test_adversarial.py::test_renamed_path_arguments_and_control_files_are_denied` |
| **B3** memory 合法 JSON 结构损坏崩溃、无快照恢复 | semantic/procedural `_load` 做完整字段类型校验；任何 `ValueError/TypeError/AttributeError/KeyError` → quarantine 当前文件 → 逐个尝试 `.snapshots` 候选 → 空默认；`Session.record_answer` 捕获记忆层全部常见异常并写 `memory_write_error`；`_load_session` 把残余 `ValueError` 转为可读错误；episodic 坏行写入 content-addressed `.corrupt-*` 并统计；去重键加入 `result`；state history 时间戳改毫秒 | `test_memory.py::TestSchemaCorruption`、`test_semantic_and_procedural_restore_from_snapshot`、`test_answer_recording_never_breaks_the_study_loop`、`test_cli_status_reports_an_unreadable_workspace`、`TestImportDeduplication` |
| **I1** 扫描器漏检 | `_norm` 增加续行拼接、引号去除、静态 home 别名展开、重定向剥离；重写 `rm`/Windows `del`/`shutdown` 模式以接受选项顺序与 `--`；新增 base64 管道/`-EncodedCommand` 解码后二次扫描 | `TestCommandScanner` 新增全部审阅变体 + base64/PowerShell 编码；良性文本回归 |
| **I2** 对抗测试自证/非端到端 | `setup` findings 用独立重算的扫描结果断言（不再自比较）；明确标注 library-level；新增变体动作、控制文件删除、生产 explain 注册表只读边界测试，并让毒材料驱动的 FakeProvider 走真实 `_explain_slice` loop 断言写/删被拒 | `test_adversarial.py` 全部 6 个用例 |
| **I3** 加载图不校验环 | `from_dict`/`load` 拒绝环、重复节点、悬空边并回退 state；`blocking_chain` 从 `seen={kp}` 开始；`topological_order` 检测环报错；`ancestors` 防递归 | `test_cyclic_graph_file_is_rejected_and_state_is_rebuilt` |
| **I4** 非 `chN` id 静默丢节点 | `GraphNode.from_dict` 先读 `chapter`，缺失时才用 `chN` 正则；无 chapter 的非 `chN` id 抛 `ValueError`；空图/部分损坏图回退 state；`unmet_prerequisites` 对缺失目标重建图 | `test_non_chapter_node_ids_are_not_silently_dropped`、`test_missing_target_is_rebuilt_from_state_not_treated_as_unlocked` |
| **I5** 考试日期全局常数、`review_first` 死配置 | `cram_score` 用章节练习债务/每日题量估算所需天数，剩余天数不足时按章节提升；`recommend` 读取 `policy.review_first`（长期=复习优先，突击=新学优先，无新章仍推荐复习） | `test_cram_order_changes_with_the_exam_runway`、`test_review_first_policy_controls_the_next_action` |
| **I6** 文件工具双重计费 | handler 内自检 `count_throttle=False`，只有 registry gate 记账；新增双层测试 | `test_registry_and_handler_share_one_throttle_slot` |
| **I7** 保留键原样传给 handler | `ToolRegistry.execute` 在授权后剥离 `risk/force/authorized/permission/grant*/token*/...` 再调用 handler | `test_reserved_model_arguments_are_stripped_before_the_handler` |
| **I8** 快照未用、同分钟去重丢事件 | 同 B3：`snapshot_candidates` 逐级恢复；state 时间戳毫秒；semantic/episodic 去重键加入 `result` | `test_semantic_and_procedural_restore_from_snapshot`、`TestImportDeduplication` |
| **I9** 审计非全量、allow 无路径、主机写不计熔断 | `PermissionGate` 默认创建 `AuditLog`；allow 决策带 `paths`；`read_jsonl(limit=0)` 返回空；熔断范围在 `WriteThrottle` 文档与报告中明确限定为模型工具写（主机侧为受信原子写） | `TestAuditCoverage`、`test_read_jsonl_limit_zero_returns_nothing` |
| **I10** grant 边界/TrashManager | `validate_grant_target` 拒绝盘根、UNC 根、home 与 home 祖先；`PermissionStore.grant_directory` 同样校验；`TrashManager` 用 `same_path`/`is_within` 做根与祖先保护，`restore` 目标必须留在 workspace 且不得进 materials | `TestGrantTargetsAndTrashFence`、`test_p1_acceptance.py::test_permissions_grant_refuses_over_broad_targets` |
| **I11** 大小写/Unicode 归一化 | 新增 `path_key`（realpath + NFC + `normcase`）与 `same_path`（存在时 `samefile`）；`is_within`/trash 根保护/路径比较统一使用 | `TestPathNormalisation` |
| **I12** `goto` 先跳转再警告 | 先算 `unmet_prerequisites`；无 `--force` 不改 `current/status/part` 也不保存，直接提示先修；`--force` 才切换并明确打印“强制跳过” | `test_p1_acceptance.py::test_next_redirects_to_the_unmet_prerequisite_with_a_reason` |

审阅 §三 的建议项顺带修复：`apply_edits` 吞 `KeyError/AttributeError`、相对 grant 值按 workspace 解析、state/grant/note 持久化前 `redact_text`、`cmd_mistakes`/`search_materials` 元数据走 `_safe_meta`、`read_jsonl(limit=0)`、`index.save_index` 改用原子写。

---

## 4. 未做项与已知限制

1. **KP 粒度仍是章级**：`GraphNode` 的 `concepts` 只做展示，BKT/先修/阻塞都按 `chN`；没有子章节 KP，也没有跨 KP 的概率传播（阻塞是确定性的先修门槛，不是贝叶斯传播）。
2. **突击模式的 `next` 不跳章**：`plan`/`order_chapters` 按“章节级时间压力×真题频率×未掌握”排序，但 `next` 对尚未开始的当前章仍优先（尊重当前进度/`goto`）。因此 `next` 本身不会主动跨章冲刺。
3. **记忆只落盘、尚未回流提示词**：三层均已结构化写入并可恢复，但教程生成/评分还没有把 semantic 错误模式与 procedural 偏好检索进上下文；没有向量检索或跨课程记忆。
4. **扫描器是确定性归一化+正则启发式**：已覆盖引号、静态变量、选项顺序、重定向、续行、Windows 变体与 base64 管道，但不做沙箱；复杂变量拼接、同形字/新措辞仍可能绕过。任何未来 exec 类工具仍应叠加 OS 级沙箱（P2）。
5. **材料只在 setup 扫描**：之后新增/修改的材料不会自动重扫；需重新 `setup`。知识图谱同理，材料变化后需 `graph --rebuild` 或重跑 setup。
6. **不真删是设计选择**：`.trash/`、`.snapshots/.expired/` 会持续增长，没有保留期清理器（避免误删）；审计 JSONL 也没有轮转/配额。
7. **无跨进程锁**：原子替换保证读者不见半文件，但两个进程同时写同一 workspace 仍可能后写覆盖先写。
8. **权限只有 CLI 管理**：无交互式审批 UI；授权/撤销需用户显式运行命令。授权按 workspace 绝对路径哈希存在用户配置目录，移动 workspace 后需要重新授权。
9. **FSRS 权重固定为默认值**：未做 per-user 参数拟合；未引入 py-fsrs 作为可选后端。
10. **BehaviorBench 尚未接入**：对抗集已在 pytest 中，但还没有并入 P3 的 BehaviorBench 评测流水线。
11. **熔断只覆盖模型工具写**：`study_state.json`、memory、setup 等主机侧写入是受信操作，走原子写+快照但不消耗模型写配额（已在 `WriteThrottle` 文档与本报告明确）。集中审计主机侧写入列入 P2。
12. **符号链接 TOCTOU 未消除**：gate 在 canonical 路径上判定，handler 随后用原路径打开；并发替换 symlink 仍有理论窗口。P2 用 `openat`/`O_NOFOLLOW`/目录 fd 收紧。
13. **`new_vs_review`/`new_share`/`review_share` 仍是策略描述字段**：`review_first` 已接入 `recommend`，但这三个比例字段尚未驱动实际排期；在 P2 的每日时间预算中消费或删除（已登记，不静默保留）。
14. **旧授权需要重新授予**：B1 修复后，旧工作区的 `<workspace>/permissions.json` 被刻意忽略（它位于模型可写区，不能作为授权根）。升级后用户需重新运行 `examprep permissions grant <dir>`；该文件本身也被 gate 设为只读。
15. **只读命令仍有受信写副作用**：`Session.load` 会回填 memory/`sync_from_state`，`cmd_plan` 末尾 `session.save()`；这些是主机侧受信写（原子+快照），不是模型写。P2 再拆分真正只读的 `status/plan` 路径。

---

## 5. P2 建议与延期登记

### 5.1 明确延期项（本轮未做，已排期）

| 项 | 理由 | 排期 |
| --- | --- | --- |
| 把 `SecureFiles`/`register_secure_file_tools` 接入生产 CLI agent 注册表（I2） | P1 的 explain 流程是只读讲解；向模型开放写文件会改变能力面，需要同步设计工具提示词、用户可见的操作确认与端到端 BehaviorBench 用例。B1/B2 已把 gate/围栏修到可安全接入的状态 | **P2 第 1 迭代**（BehaviorBench 接入之前）；验收：毒材料驱动的真实 `_explain_slice`/agent loop 端到端拦截测试 |
| 主机侧关键写入纳入统一写审计/熔断（I9） | 需要先定义“主机写”与“模型写”的配额语义，避免 setup/memory 自检把熔断打满；当前以文档明确范围并保留原子写+快照 | P2 安全纵深迭代 |
| `openat`/`O_NOFOLLOW` 消除符号链接 TOCTOU | 跨平台实现成本高；当前 realpath fence 已阻止正常路径逃逸 | P2 安全纵深迭代 |
| 消费 `new_vs_review` 比例（I5 遗留） | 单动作推荐器先按 `review_first` 决策；比例字段需要每日时间预算/题量模型配合 | P2 突击调度修正 |
| 审计 JSONL 轮转/配额/哈希链 | 属于运维与防篡改增强，不影响 P1 契约 | P2 服务化迭代 |

### 5.2 P2 建议

1. **KP 细化与图谱增强**：从标题/编号/引用抽取子章节 KP，BKT 挂到子 KP；先修边引入置信度与人工修订审计；为跨 KP 掌握度传播做实验（先修达标后再解锁目标 KP）。
2. **记忆回流**：在 `next`/`explain` 上下文中注入 semantic 错误聚类与 procedural 偏好（例如“该生常混淆 LIFO/FIFO，优先对比讲解”），再做可选的向量检索；所有注入仍走 material fence 与长度预算。
3. **FSRS 生产化**：把 py-fsrs 作为**可选**依赖做交叉校验与权重拟合（默认仍用内置实现），增加复习日志导出与 retention 校准。
4. **安全纵深**：给未来 exec 工具加 OS 级沙箱/容器；扫描器增加 Unicode 归一化对抗样例（同形字、零宽、更多编码）与 fuzz 测试；审计日志轮转 + 配额 + 哈希链防篡改。
5. **服务化（PLAN P2）**：FastAPI + MCP Server 暴露 `next/plan/graph/permissions`；多实例下的 workspace 锁、集中审计与写熔断。
6. **突击调度修正**：允许 `next` 在“当前章尚未开始”时按冲刺分跨章选择；消费 `new_vs_review` 比例并加入每日时间预算。
7. **BehaviorBench**：把 `tests/test_adversarial.py` 与注入/越权用例并入 BehaviorBench，建立攻击族回归门槛（100% 拦截为发布阻断项）；在此之前先完成上面的生产工具面接线延期项。

---

## 6. 关键文件

```
examprep_agent/security/        risk, paths, permissions, filetools, trash, audit, commands, atomicio
examprep_agent/memory/          episodic, semantic, procedural, store
examprep_agent/learning/        graph, planner, learner, scheduler, modes
examprep_agent/core/session.py  三层记忆/图谱/安全上下文/日程计数接线
examprep_agent/cli.py           next/plan/status/goto/graph/permissions/setup 接线
tests/test_security.py          权限闸门/控制文件只读/路径参数/围栏/trash/审计/熔断/扫描器
tests/test_adversarial.py       毒材料四类攻击（library-level gate）+ 生产注册表只读边界
tests/test_graph_planner.py     图谱/环拒绝/先修阻塞/due 优先/突击排序/review_first
tests/test_memory.py            三层落盘/结构损坏降级/快照恢复/同分钟去重
tests/test_p1_acceptance.py     任务书端到端冒烟 + permissions/graph/goto CLI
```

运行：`/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m pytest -q`
