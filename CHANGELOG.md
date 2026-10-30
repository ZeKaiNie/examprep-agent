# Changelog

All notable changes to `examprep-agent`. This project follows
[Semantic Versioning](https://semver.org/) once a public release is tagged.

## [0.1.0] — 2026-10-30

First release candidate. Four development phases were completed in one
repository, each with cross-review fixes and an offline test suite.

### P0 — foundation

- Package scaffolding, GitHub Actions CI, pytest conventions.
- Deterministic content pipeline ported from universal-examprep-skill (MIT,
  © ZeKaiNie): PDF/DOCX/PPTX/MD/TXT/HTML extraction, chapter grouping,
  question/answer pairing, figure cropping, BM25 retrieval, material fencing
  and prompt-injection heuristics, atomic `study_state.json`.
- Agent kernel: provider abstraction (OpenAI-compatible + offline
  `FakeProvider`), plan–act–observe loop, tool registry, hard step/token
  budgets, JSONL event logs.
- Context engine: token estimation, three-compartment budgets, incremental
  compaction keeping the recent tail verbatim.
- Durable sessions: study state + learner + FSRS cards + `session.json`
  checkpoints with atomic writes and snapshots.
- Learning layer: real BKT and FSRS-4.5, long-term/cram policies, mastery-gated
  `done`, skill-equivalent CLI with `📍` next-step guidance.

### P1 — learner model and long-term memory

- Prerequisite knowledge graph with sequence/reference edges, cycle-safe user
  edits, and blocking of unmet prerequisites.
- FSRS due queue feeding `plan`/`next`; daily new-learning budget; cram-mode
  urgency ordering.
- Three-layer memory: episodic JSONL, semantic profile, procedural
  preferences; schema validation and snapshot recovery.
- Permission gate with L0–L3 risk tiers, grants stored outside the
  model-writable workspace, command scanner, internal-path protection, audit
  JSONL and adversarial tests.

### P2 — concurrency and service layer

- `StudyService` shared by CLI, FastAPI and MCP; structured errors and exit
  codes mapped to HTTP semantics.
- Thread-pool ingestion (extract/chapters/figures/index) with per-file failure
  isolation, deterministic merge order and JSONL progress events.
- Isolated sub-agents for retrieval/grading: independent context budgets,
  summary sanitisation (including cross-line injection), parental risk ceiling.
- Workspace file locking; FastAPI REST surface; MCP stdio server with seven
  tools; cost aggregation from existing usage events.
- LangGraph spike under `spikes/` (isolated, not a runtime dependency); the
  self-built kernel was retained.

### P3 — four-layer benchmarks and release preparation

- **GroundingBench**: closed-book / raw-file / agent arms on 34 in-material
  questions plus 10 out-of-material probes, using the **same deterministic
  reader and scorer** for all three arms. The agent arm answers only through
  the production `StudyService.ask` pipeline; gold answers are read only by the
  scorer; probe refusals come from a global, gold-free evidence-support check.
- **BehaviorBench**: nine **material-induced** attacks (poisoned material in the
  agent context; a scripted provider parses the action from the material and
  sends it through the real `run_agent` loop) plus five supplementary
  static/direct gate checks; 16 positive teaching-loop commands and 3 negative
  controls counted separately; 6 independent cross-command invariants
  (complete footer list including `done`, cumulative state, read-only
  non-mutation, open-mistake accounting, poisoned-course fence integrity).
- **LongHorizon Sim**: 30-day simulated learner (23 study days + 7-day
  retention interval). The hidden learner is constructed only in a child
  process; the agent gets a pipe-backed `teach / quiz -> bool / restudy`
  façade. `hidden_state_isolated` is computed from actual read/tamper attempts.
  Both arms share the available time budget; metrics include pretest, learning
  gain = post − pre, gain per study/budget hour, no-review/no-restudy
  counterfactuals, an equal-actual-time intervention and delayed-test 95% CI.
- **TutorRubric**: 16 fixed scenarios (good / medium single-signal ablations /
  flawed / exam-style bypass); citation pages, claim terms, test relevance and
  next-step executability are verified; `bypass_full_score_count` must be 0;
  optional `--judge` hook (skipped without an API key, key resolved before the
  offline sandbox).
- Performance probes: ingest throughput/serial-vs-threaded timing, core service
  command latency, machine-stamped results.
- `python -m benchmarks.run_all --out benchmarks/results/` writes
  `results.json`, `BENCH_REPORT.md` and `summary.md`; report numbers are
  rendered from the same run's JSON and a canonical run also syncs the marked
  README/P3_REPORT summary blocks.
- Bilingual README with the benchmark summary table and architecture diagram;
  CHANGELOG; ARCHITECTURE.md synced to the P2/P3 structure; `P3_REPORT.md`.

### Release notes

- Version is `0.1.0`; core runtime dependency remains `pydantic`.
- Optional extras: `serve` (FastAPI), `mcp`, `pdf`, `spike`, `dev`.
- No real-person pilot, no calibrated LLM judge and no cross-machine
  performance claim is included in this release candidate.
- `research/skill-repo/` remains read-only and unmodified.
