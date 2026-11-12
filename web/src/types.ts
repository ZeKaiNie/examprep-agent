// 后端事件与接口数据的类型。对应 packages/app/src/examprep/study/events.py 与 ingest/events.py。

export type State = "TEACH" | "CHECK" | "PRACTICE" | "SELF" | "DONE";
export type TutorKind = "teach" | "reteach" | "check" | "feedback" | "hint" | "answer" | "wrapup";

export interface CitationRef {
  label: string;
  evidence_id: string;
  file: string;
  page: number;
}

export interface FigureRef {
  file: string;
  page: number;
  path: string; // 相对课程数据目录
}

export interface ChapterRow {
  n: number;
  title: string;
  status: string;
  parts_done: number;
  parts_total: number;
  questions: number;
  correct: number;
}

export type StudyEvent =
  | { type: "state"; state: State; chapter: number }
  | { type: "figures"; figures: FigureRef[] }
  | { type: "tutor_delta"; text: string }
  | { type: "tutor_message"; kind: TutorKind; text: string; citations: CitationRef[]; unknown: string[] }
  | { type: "verified"; total: number; supported: number; partial: number; unsupported: number; text: string }
  | {
      type: "question";
      qid: string;
      chapter: number | null;
      qtype: string;
      stem: string;
      options: string[];
      points: number | null;
      source_file: string;
      source_page: number;
      figures: FigureRef[];
      has_answer: boolean;
      index: number;
      total: number;
    }
  | {
      type: "graded";
      qid: string;
      verdict: string;
      score: number | null;
      grader: string;
      reason: string | null;
      error_type: string | null;
      reference: string | null;
      reference_source: string | null;
      answer_figures: FigureRef[];
    }
  | { type: "refused"; query: string; text: string }
  | { type: "note"; note_type: string; chapter: number | null; text: string }
  | { type: "chapter_done"; chapter: number; status: string }
  | { type: "course_done" }
  | {
      type: "progress";
      course: string;
      current: number;
      mode: string;
      days_left: number | null;
      chapters: ChapterRow[];
      open_mistakes: number;
    }
  | { type: "compacted"; manual: boolean; ok: boolean; tokens_before: number; tokens_after: number; summary: string }
  | { type: "suggestions"; items: string[] }
  | { type: "notice"; level: "info" | "warning" | "error"; text: string };

export type StudyEventOf<T extends StudyEvent["type"]> = Extract<StudyEvent, { type: T }>;

export interface ChapterSummary {
  n: number;
  title: string;
  sources: string[];
  questions: number;
}

export interface IngestReport {
  course_id: string;
  name: string;
  root: string;
  language: string;
  files: Record<string, number>;
  chapters: ChapterSummary[];
  questions: number;
  answered: number;
  figures: number;
  crops: number;
  scan_pages: number;
  evidence: number;
  parsed: number;
  reused: number;
  removed: number;
  failed: number;
  warnings: string[];
  injection: { file: string; pages: number[] }[];
  seconds: number;
}

export type IngestEvent =
  | { type: "ingest_started"; course_id: string; name: string; files: number; new: number; changed: number; unchanged: number; removed: number }
  | { type: "file_started"; rel: string }
  | { type: "file_done"; rel: string; kind: string; pages: number; figures: number; seconds: number }
  | { type: "file_failed"; rel: string; error: string }
  | { type: "stage_started"; stage: string }
  | { type: "stage_done"; stage: string; detail: string }
  | { type: "ingest_done"; report: IngestReport };

export interface Course {
  id: string;
  name: string;
  language: string;
  root: string;
  updated_at: string;
}

export interface CourseDetail extends Course {
  chapters: { n: number; title: string; sources: string[]; questions: number }[];
  questions: number;
  figures: number;
  materials: { file: string; kind: string; pages: number; error: string | null }[];
}

export interface PageEvidence {
  id: string;
  chapter: number;
  start: number | null;
  end: number | null;
  text: string;
}

export interface PageData {
  file: string;
  page: number;
  pages: number;
  text: string;
  is_scan: boolean;
  width: number | null;
  height: number | null;
  renderable: boolean;
  evidence: PageEvidence[];
}

export interface StudyState {
  open: boolean;
  busy: boolean;
  chapter?: number;
  state?: State;
  awaiting?: "answer" | "check" | null;
  context_tokens?: number;
  compact_limit?: number;
  auto_compact?: boolean;
  progress?: StudyEventOf<"progress"> | null;
}

export type TranscriptRow =
  | { seq: number; role: "student"; text: string }
  | { seq: number; role: "agent"; event: StudyEvent };

export interface Health {
  version: string;
  model_configured: boolean;
}
