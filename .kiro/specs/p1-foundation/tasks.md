# P1 地基：任务

每项完成后勾选。括号里是对应需求。

## 1. 仓库与工程化（R1）

- [ ] 1.1 `git init`；`.gitignore`、`.editorconfig`、`LICENSE`（MIT）、`README.md` 初版
- [ ] 1.2 uv workspace 根 `pyproject.toml`（package = false）、`.python-version`；`packages/llm`、`packages/agent` 骨架（hatchling、src 布局、`py.typed`）
- [ ] 1.3 ruff、pyright（strict）、pytest、coverage 配置；`uv sync` 通过
- [ ] 1.4 版本号一致性测试 `tests/test_versions.py`
- [ ] 1.5 GitHub Actions：3 系统 × Python 3.11/3.12 跑 ruff、pyright、pytest

## 2. kaopu_llm（R2–R5）

- [ ] 2.1 `types.py`、`events.py`、`errors.py`：消息、内容块、请求、用量、事件、错误；测试序列化往返与判别字段
- [ ] 2.2 `cancel.py`、`clock.py`：CancelToken、cancellable；测试父子令牌、回调注销、取消中断
- [ ] 2.3 `sse.py`：SSE 解码；测试规范用例与随机切块
- [ ] 2.4 `accumulator.py`、`tokens.py`：增量拼装、token 估算与截断；测试
- [ ] 2.5 `registry.py`、`config.py`：能力表、价格、成本、provider 预设；测试覆盖/默认值/成本计算
- [ ] 2.6 `protocols/_http.py`：错误映射、retry-after 解析；参数化测试
- [ ] 2.7 OpenAI 兼容协议 + 回放夹具（文本、推理、工具调用、usage、DeepSeek 缓存字段、流中错误）
- [ ] 2.8 Anthropic 协议 + 回放夹具（thinking + signature、工具调用、缓存用量、overloaded 错误事件）
- [ ] 2.9 Gemini 协议 + 回放夹具（函数调用、thought、thoughtSignature、usageMetadata）
- [ ] 2.10 `testing.py`：FakeProvider、FakeTurn、FakeClock
- [ ] 2.11 网关：重试、限流、熔断、降级、取消、记账；确定性测试

## 3. kaopu_agent（R6–R10）

- [ ] 3.1 `events.py`、`tools.py`：工具定义、schema 生成、装饰器；测试
- [ ] 3.2 `hooks.py`、`budget.py`、`loop.py`：循环、并行工具、钩子；测试
- [ ] 3.3 steer / follow_up 队列；测试
- [ ] 3.4 取消（流中、工具中）与预算（轮数、工具数、重复）；测试
- [ ] 3.5 `session/`：EventStore、内存与 SQLite 实现、投影、分叉、Recorder；测试
- [ ] 3.6 `lane.py`：串行、并行、四种策略；确定性测试
- [ ] 3.7 `subagent.py`：submit_result、提醒、超时、错误隔离；测试
- [ ] 3.8 `context.py`：分层组装与预算；测试

## 4. 验收

- [ ] 4.1 `tests/test_acceptance_replay.py`：三种协议跑通同一工具调用脚本
- [ ] 4.2 `tests/test_live.py`：在线测试（有 Key / Ollama 时运行，否则如实标注未验证）
- [ ] 4.3 覆盖率 ≥ 85%；ruff、pyright 零错误；Python 3.11 与 3.12 本地都跑通
- [ ] 4.4 更新 `project-memory.md`
