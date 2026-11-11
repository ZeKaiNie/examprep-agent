# 样例课程

| 目录 | 内容 | 许可 | 是否入库 |
|---|---|---|---|
| `zh-data-structures/` | 数据结构（中文）：3 章讲义 + 1 份作业 + 1 份模拟卷 | MIT（来自 Exam Cram Coach Skill，同一作者） | 是 |
| `mit-6006/` | MIT OCW 6.006（2020 春）：6 份讲义 + Quiz 1 + 答案 | CC BY-NC-SA 4.0 | 否，用 `fetch.py` 下载 |
| `yale-psyc110/` | Open Yale PSYC 110：4 讲文字稿 | CC BY-NC-SA 3.0 | 否，用 `fetch.py` 下载 |

```bash
uv run python samples/fetch.py mit-6006
uv run examprep ingest samples/mit-6006
```
