// rehype 插件：把段落里“🟡 …。”这一句包进 <span class="extra-inline">，显示得更轻。
// 核对员按句加 🟡（见 agents/verifier.py 的 apply_flags），所以 🟡 常常出现在段落中间，不能只看段首。

import type { Element, ElementContent, Root, Text } from "hast";

const MARK = "🟡";
const END = /[。！？；!?]/;

function splitText(node: Text): ElementContent[] {
  const value = node.value;
  if (!value.includes(MARK)) return [node];
  const out: ElementContent[] = [];
  let pos = 0;
  while (pos < value.length) {
    const start = value.indexOf(MARK, pos);
    if (start < 0) {
      out.push({ type: "text", value: value.slice(pos) });
      break;
    }
    if (start > pos) out.push({ type: "text", value: value.slice(pos, start) });
    const rest = value.slice(start + MARK.length);
    const m = END.exec(rest);
    const end = m ? start + MARK.length + m.index + 1 : value.length;
    out.push({
      type: "element",
      tagName: "span",
      properties: { className: ["extra-inline"] },
      children: [{ type: "text", value: value.slice(start, end) }],
    });
    pos = end;
  }
  return out;
}

function walk(node: Root | Element, inCode: boolean): void {
  const next: (Root | Element)["children"] = [];
  for (const child of node.children) {
    if (child.type === "text" && !inCode) {
      next.push(...splitText(child));
    } else {
      if (child.type === "element") walk(child, inCode || child.tagName === "code" || child.tagName === "pre");
      next.push(child as ElementContent);
    }
  }
  node.children = next as typeof node.children;
}

export function rehypeExtraMark() {
  return (tree: Root): void => {
    walk(tree, false);
  };
}
