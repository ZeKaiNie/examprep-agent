# P2 资料引擎：设计

依据：`requirements.md`（R1–R6）。原则同 P1：纯函数、可注入、可确定性测试；代码注释用中文。

## 1. 模块与文件

```
packages/app/src/examprep/
├ ingest/
│  ├ text.py        清洗、分词、分段打包、中文数字（移植 Skill text.py）
│  ├ guard.py       注入检测、围栏（移植 guard.py；不可见字符写成 \u 转义）
│  ├ extract.py     Page / Source 数据类、各格式读取、页眉页脚去除、分类、文件列表（移植 extract.py）
│  ├ chapters.py    Chapter 数据类与三条章节规则（移植 chapters.py）
│  ├ questions.py   切题、配对、题型、章节归属（移植 questions.py）
│  ├ grading.py     answer_key / grade_answer（从 questions.py 拆出）
│  ├ figures.py     PDF 图区检测、渲染裁剪、PNG 编码、Office 内嵌图、题目裁图（移植 figures.py 与 cli.py 的裁图逻辑）
│  ├ parse.py       parse_file：单个文件的完整解析（文本 + 图），进程池里运行，输入输出都可 pickle
│  ├ events.py      导入进度事件与导入报告（Pydantic）
│  └ pipeline.py    Ingestor：扫描、哈希、增量判定、任务调度、课程级重建、落库、事件
├ knowledge/
│  ├ bm25.py        BM25（k1=1.5, b=0.75）
│  └ evidence.py    证据块切分（约 600 字）、稳定 id、字符区间、检索
├ store/
│  ├ models.py      SQLAlchemy 2 声明式模型
│  ├ db.py          Database：异步引擎、SQLite WAL、建表、会话
│  └ jobs.py        任务表的入队、领取（租约）、完成、失败、过期回收
└ cli.py            examprep ingest / search
```

## 2. 移植约定

- Skill 的类改成 `@dataclass(slots=True)`；字典型题目改成 `QuestionItem` 数据类，字段名与 Skill 输出 JSON 一致（`number`、`label`、`question`、`options`、`answer`、`type`、`points`、`source`、`answer_source`、`chapter`、`chapter_guessed`、`id`）。
- 正则、阈值、顺序与 Skill v5.3 逐条一致；行为差异只允许是修 bug，并在测试里写明。
- pypdfium2 没有完整类型，`extract.py`、`figures.py` 文件头关闭 pyright 的 unknown 类报告，其余文件保持 strict。

## 3. 解析（进程池）

```python
@dataclass
class ParseRequest:   root: str; rel: str; sha256: str; figure_dir: str   # figure_dir：本文件图片输出目录
@dataclass
class ParsedFile:     rel; sha256; kind; pages: list[Page]; empty_pages; warnings; error
                      figures: list[FigureRecord]          # 文件级图（kind=figure）
                      layout: dict[int, PageLayout]         # PDF 每页尺寸、扫描标记、图框、文字框（供题目裁图）
def parse_file(req: ParseRequest) -> ParsedFile             # 顶层函数，可 pickle
```

- `extract_source` 抽文本 → PDF 时 `analyze_pdf` + 渲染图区 → 用扫描页结果回填 `Page.scan`。
- 图片文件名：`<stem>_p<页>_<序号>.png`，放在 `figures/<sha 前 12 位>/` 下；文件未变时目录原样复用。
- 一个文件失败只记 `error`，不抛出。

## 4. 增量与任务

- `material` 表按 `(tenant_id, course_id, rel_path)` 唯一，存 `sha256`、`kind`、`status`（active / removed）、`error`。
- 导入时对每个文件算 sha256：新增或 sha 变化 → 解析任务；sha 未变且上次成功 → 从数据库读回页与图；不在目录里 → `status=removed`。
- 文件级结果（page、文件级 figure）在该文件解析成功后整体替换；课程级结果（chapter、evidence、question、题目裁图）每次导入删掉重建。
- `job` 表：`(course_id, rel_path, sha256)` 唯一；状态 pending / running / done / failed，`attempts`，`lease_until`。
  - 开始导入：同课程 `running` 且租约未过期 → 抛 `IngestBusyError`；租约已过期 → 回收为 pending。
  - 领取时置 running、租约 = 现在 + 10 分钟；完成置 done；失败置 failed 并记录错误（下次导入若文件仍是同一 sha，最多再试 3 次）。
- 进程池：`ProcessPoolExecutor(max_workers=N)`，`workers=0` 时在当前进程顺序执行（测试与小课程）。

## 5. 课程级重建

1. 用全部 active 文件的 `Source`（页文本 + scan 标记）按 Skill 顺序（自然排序）重建章节；可疑或过长的章节标题按 Skill 规则替换或截断。
2. 题目抽取与配对、章节归属，编号 `q001…`。
3. 题目裁图：对每道题的题面与答案所在 PDF，用 layout 计算区域（Skill `_segments_for`），只有区域与图重叠才裁；输出到 `crops/`，每次重建前清空。
4. 证据块：对每章的每个块按约 600 字切分；id = `E` + sha1(`rel|page|序号|文本`) 前 10 位；字符区间为该块在页文本中的位置（找不到时为空）。
5. 语言：`--lang` 优先，否则按全部章节文本判断中英文。
6. 注入告警：逐页 `flagged_lines`，记录有可疑文本的页号。

## 6. 存储

| 表 | 主要字段 |
|---|---|
| course | id, tenant_id, name, root, language, created_at, updated_at |
| material | id, tenant_id, course_id, rel_path, sha256, kind, status, error, warnings(JSON), pages_count, updated_at |
| page | id, tenant_id, material_id, n, text, is_scan, width, height, layout(JSON) |
| chapter | id, tenant_id, course_id, n, title, sources(JSON), figures(JSON) |
| evidence | id(E…), tenant_id, course_id, material_id, rel_path, page, chapter_n, ordinal, text, char_start, char_end, tokens |
| figure | id, tenant_id, course_id, material_id, rel_path, page, kind(figure/question/answer), path, box(JSON), width, height, question_id |
| question | id, tenant_id, course_id, qid, number, label, type, stem, options(JSON), answer, points, chapter_n, chapter_guessed, source(JSON), answer_source(JSON), is_exam |
| job | id, tenant_id, course_id, rel_path, sha256, status, attempts, lease_until, error, created_at, updated_at |

- SQLite 打开时设 `journal_mode=WAL`、`foreign_keys=ON`；P2 用 `create_all`，Alembic 迁移在 P3。
- 所有查询带 `tenant_id`，本地固定 `local`。

## 7. 事件与报告

```python
IngestStarted(course_id, name, files, new, changed, unchanged, removed)
FileStarted(rel) / FileDone(rel, kind, pages, figures, seconds) / FileFailed(rel, error)
StageStarted(stage) / StageDone(stage, detail)        # stage: parse / chapters / questions / crops / evidence / save
IngestDone(report: IngestReport)
```

`IngestReport`：course_id、name、language、files（按 kind 计数）、chapters（n、title、sources、questions）、questions、answered、figures、crops、scan_pages、parsed、reused、removed、warnings、injection_pages、seconds。

## 8. 测试

| 范围 | 方法 |
|---|---|
| 移植 | Skill 的 text、extract、chapters、questions、labels、figures、guard、index、判分测试逐条改写为 pytest |
| 中文样例 | `samples/zh-data-structures` 入库；golden 快照（由 Skill 输出生成，只存元数据） |
| MIT 样例 | `samples/fetch.py` 下载到 `samples/mit-6006/`（gitignore）；存在时对比 golden，不存在时跳过 |
| 增量 | 二次导入解析 0 个文件；改一个文件只解析它；删一个文件后其题目消失；崩溃遗留的过期任务被回收 |
| 并发 | `workers=2` 走真实进程池 |
| CLI | ingest 文本与 JSON 输出、search 命中与未命中 |

## 9. 暂不做

LLM 任务（知识点、先修关系、题目标注、无编号题兜底）放 P4；SSE 接口与 Alembic 放 P3；v4.3 难度分与 OCR 放后续。
