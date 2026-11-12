# P3a 补充：上下文压缩 设计

详细规则见 `docs/design/04-agent-core.md` 2.3.1；这里记实现落点。

| 模块 | 改动 |
|---|---|
| `settings.py` | `CompactionPolicy(auto, ratio, max_tokens)`，`threshold(window)`；`load_compaction()` 读 `[compaction]` 并校验；`load_settings()` 忽略这一节（不进网关配置） |
| `agents/prompts.py` | `compact(language)`：压缩指令，作为最后一条用户消息 |
| `study/context.py` | `system_messages(..., recap, summary)`：L2b 前几章小结、L6 压缩摘要，都在系统消息里，段内不变；`recap_text()` 去掉“（文件 p.N）” |
| `study/events.py` | `Compacted` 事件 |
| `pedagogy/intent.py` | `compact` 意图（明确说法任何状态；单说“压缩”只在不等作答时） |
| `study/session.py` | `_prepare_segment()`（换章开新段 / 超阈值自动压缩或开新段）、`compact()`、`_compact()`、`_new_segment(reason, summary)`、`_recap()`、`compact_limit()`、`segment_tokens()`；`segment` 事件带 `reason`、`summary`；`restore_segment()` 返回摘要 |
| `study/console.py`、`cli.py` | 显示压缩结果和阈值；`--no-auto-compact` |

缓存：压缩请求的消息 = 上一次 Tutor 请求的全部消息 + 模型输出 + 压缩指令，前缀逐字节相同（测试断言）。压缩后新段的系统消息变了，下一轮是一次冷启动，之后照常只追加。

压缩请求走 `role="tutor"`：必须和讲解同一个模型、同样的思考参数，才能复用缓存。
