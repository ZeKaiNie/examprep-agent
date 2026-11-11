# P1 地基：任务

每项完成后勾选。括号里是对应需求。

## 1. 仓库与工程化（R1）

- [x] 1.1 `git init`；`.gitignore`、`.editorconfig`、`LICENSE`（MIT）、`README.md` 初版
- [x] 1.2 uv workspace 根 `pyproject.toml`（package = false）、`.python-version`；`packages/llm`、`packages/agent` 骨架（hatchling、src 布局、`py.typed`）
- [x] 1.3 ruff、pyright（strict）、pytest、coverage 配置；`uv sync` 通过
- [x] 1.4 版本号一致性测试 `tests/test_versions.py`
- [x] 1.5 GitHub Actions：3 系统 × Python 3.11/3.12 跑 ruff、pyright、pytest（已写好 workflow；尚无远程仓库，未在 GitHub 上实际运行）

## 2. kaopu_llm（R2–R5）

- [x] 2.1 `types.py`、`events.py`、`errors.py`：消息、内容块、请求、用量、事件、错误；测试序列化往返与判别字段
- [x] 2.2 `cancel.py`、`clock.py`：CancelToken、cancellable；测试父子令牌、回调注销、取消中断
- [x] 2.3 `sse.py`：SSE 解码；测试规范用例与随机切块
- [x] 2.4 `accumulator.py`、`tokens.py`：增量拼装、token 估算与截断；测试
- [x] 2.5 `registry.py`、`config.py`：能力表、价格、成本、provider 预设；测试覆盖/默认值/成本计算
- [x] 2.6 `protocols/_http.py`：错误映射、retry-after 解析；参数化测试
- [x] 2.7 OpenAI 兼容协议 + 回放夹具（文本、推理、工具调用、usage、DeepSeek 缓存字段、流中错误）
- [x] 2.8 Anthropic 协议 + 回放夹具（thinking + signature、工具调用、缓存用量、overloaded 错误事件）
- [x] 2.9 Gemini 协议 + 回放夹具（函数调用、thought、thoughtSignature、usageMetadata）
- [x] 2.10 `testing.py`：FakeProvider、FakeTurn、FakeClock
- [x] 2.11 网关：重试、限流、熔断、降级、取消、记账；确定性测试

## 3. kaopu_agent（R6–R10）

- [x] 3.1 `events.py`、`tools.py`：工具定义、schema 生成、装饰器；测试
- [x] 3.2 `hooks.py`、`budget.py`、`loop.py`：循环、并行工具、钩子；测试
- [x] 3.3 steer / follow_up 队列；测试
- [x] 3.4 取消（流中、工具中）与预算（轮数、工具数、重复）；测试
- [x] 3.5 `session/`：EventStore、内存与 SQLite 实现、投影、分叉、Recorder；测试
- [x] 3.6 `lane.py`：串行、并行、四种策略；确定性测试
- [x] 3.7 `subagent.py`：submit_result、提醒、超时、错误隔离；测试
- [x] 3.8 `context.py`：分层组装与预算；测试

## 4. 验收

- [x] 4.1 `tests/test_acceptance_replay.py`：三种协议跑通同一工具调用脚本
- [x] 4.2 `tests/test_live.py`：在线测试——2026-11-11 用 DeepSeek `deepseek-flash`（V4.1-Flash）通过（两个用户回合、工具调用、思考模式推理内容回传）；Anthropic、Gemini、Ollama 无 Key / 无服务，**未验证**
- [x] 4.3 覆盖率 ≥ 85%；ruff、pyright 零错误；Python 3.11 与 3.12 本地都跑通
- [x] 4.4 更新 `project-memory.md`
