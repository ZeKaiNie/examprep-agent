// 讲解文本渲染：Markdown（表格、代码、列表）+ 可点的出处 + 资料以外的补充（🟡）显示得更轻。

import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CITE_PREFIX, linkify, type CiteTarget } from "../citations";
import { rehypeExtraMark } from "../extraMark";
import type { CitationRef } from "../types";

export const EXTRA_MARK = "🟡";

function startsWithMark(children: ReactNode): boolean {
  const first = Children.toArray(children)[0];
  if (typeof first === "string") return first.trimStart().startsWith(EXTRA_MARK);
  if (isValidElement<{ children?: ReactNode }>(first)) return startsWithMark(first.props.children);
  return false;
}

interface Props {
  text: string;
  citations?: readonly CitationRef[];
  onCite?: (target: CiteTarget) => void;
}

export function Markdown({ text, citations = [], onCite }: Props) {
  const { markdown, targets } = linkify(text, citations);
  const components: Components = {
    a({ href, children }) {
      if (href?.startsWith(CITE_PREFIX)) {
        const target = targets[Number(href.slice(CITE_PREFIX.length))];
        return (
          <button
            type="button"
            className="cite"
            title="在右侧打开原文"
            disabled={!target || !onCite}
            onClick={() => target && onCite?.(target)}
          >
            {children}
          </button>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    p({ children }) {
      return <p className={startsWithMark(children) ? "extra" : undefined}>{children}</p>;
    },
    li({ children }) {
      return <li className={startsWithMark(children) ? "extra" : undefined}>{children}</li>;
    },
  };
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeExtraMark]} components={components}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
