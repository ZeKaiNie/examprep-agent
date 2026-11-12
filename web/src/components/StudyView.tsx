// 学习页：左边进度，中间对话，右边原文阅读器。

import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent } from "react";

import { ApiError, api } from "../api";
import { initialChat, reduce } from "../chat";
import type { CiteTarget } from "../citations";
import type { StudyState } from "../types";
import { ChatItemView } from "./ChatItem";
import { ProgressPanel } from "./ProgressPanel";
import { Reader } from "./Reader";

interface Props {
  cid: string;
  onBack: () => void;
}

export function StudyView({ cid, onBack }: Props) {
  const [chat, dispatch] = useReducer(reduce, initialChat);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<string | null>(null); // 打不开会话（没配置模型 / 别处在学）
  const [state, setState] = useState<StudyState | null>(null);
  const [cite, setCite] = useState<CiteTarget | null>(null);
  const [input, setInput] = useState("");
  const bottom = useRef<HTMLDivElement | null>(null);

  const refreshState = useCallback(() => {
    api
      .state(cid)
      .then((st) => {
        setState(st);
        if (st.progress) dispatch(st.progress); // 每轮结束后刷新左侧进度
      })
      .catch(() => undefined);
  }, [cid]);

  const run = useCallback(
    async (job: () => Promise<void>) => {
      setBusy(true);
      try {
        await job();
      } catch (e) {
        dispatch({ type: "local_error", text: e instanceof Error ? e.message : String(e) });
      } finally {
        setBusy(false);
        refreshState();
      }
    },
    [refreshState],
  );

  useEffect(() => {
    let alive = true;
    setBlocked(null);
    (async () => {
      setBusy(true);
      try {
        // 刷新页面前发出的那一轮可能还在后台跑：等它跑完，对话记录才完整
        for (let i = 0; i < 60 && alive; i++) {
          const st = await api.state(cid);
          if (!st.busy) break;
          await new Promise((r) => setTimeout(r, 500));
        }
        const rows = await api.transcript(cid);
        if (!alive) return;
        dispatch({ type: "restore", rows });
        await api.open(cid, (ev) => alive && dispatch(ev));
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && (e.status === 503 || e.status === 423)) setBlocked(e.message);
        else dispatch({ type: "local_error", text: e instanceof Error ? e.message : String(e) });
      } finally {
        if (alive) {
          setBusy(false);
          refreshState();
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [cid, refreshState]);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [chat.items]);

  const send = (text: string) => {
    const t = text.trim();
    if (!t || busy) return;
    setInput("");
    dispatch({ type: "student", text: t });
    void run(() => api.message(cid, t, dispatch));
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    send(input);
  };

  const assetUrl = (path: string) => api.assetUrl(cid, path);

  return (
    <div className={`study${cite ? " with-reader" : ""}`}>
      <ProgressPanel
        progress={chat.progress}
        state={state}
        busy={busy}
        onCompact={() => void run(() => api.compact(cid, dispatch))}
      />
      <section className="chat" aria-label="对话">
        <div className="chat-top">
          <button type="button" className="link" onClick={onBack}>
            ← 课程列表
          </button>
        </div>
        {blocked && <div className="notice error blocked">{blocked}</div>}
        <div className="items">
          {chat.items.map((item, i) => (
            <ChatItemView key={i} item={item} assetUrl={assetUrl} onCite={setCite} />
          ))}
          {busy && chat.items.at(-1)?.kind !== "tutor" && <div className="thinking">思考中…</div>}
          <div ref={bottom} />
        </div>
        <div className="composer">
          {chat.suggestions.length > 0 && !busy && (
            <div className="suggestions">
              {chat.suggestions.map((s) =>
                s.startsWith("（") ? (
                  <span key={s} className="hint-line">
                    {s}
                  </span>
                ) : (
                  <button key={s} type="button" onClick={() => send(s)}>
                    {s}
                  </button>
                ),
              )}
            </div>
          )}
          <form onSubmit={onSubmit}>
            <textarea
              value={input}
              placeholder={blocked ? "暂时不能学习" : "说“继续”，回答问题，或者直接提问（Enter 发送，Shift+Enter 换行）"}
              disabled={!!blocked}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send(input);
                }
              }}
              rows={2}
            />
            {busy ? (
              <button type="button" onClick={() => void api.cancel(cid)}>
                停止
              </button>
            ) : (
              <button type="submit" disabled={!input.trim() || !!blocked}>
                发送
              </button>
            )}
          </form>
        </div>
      </section>
      {cite && <Reader cid={cid} target={cite} onClose={() => setCite(null)} />}
    </div>
  );
}
