// Server-Sent Events 增量解析。网络分块可能切在任何位置（行中间、多字节字符中间），所以按字节解码、按空行切事件。

export interface SSEMessage {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";
  private readonly decoder = new TextDecoder("utf-8");

  /** 喂一块字节，返回其中已经完整的事件。 */
  feed(chunk: Uint8Array): SSEMessage[] {
    this.buffer += this.decoder.decode(chunk, { stream: true }).replace(/\r\n?/g, "\n");
    return this.drain();
  }

  /** 流结束：解码剩余字节，并把最后一个没有空行收尾的事件也交出去。 */
  end(): SSEMessage[] {
    this.buffer += this.decoder.decode();
    const out = this.drain();
    if (this.buffer.trim()) {
      const last = parseBlock(this.buffer);
      if (last) out.push(last);
    }
    this.buffer = "";
    return out;
  }

  private drain(): SSEMessage[] {
    const out: SSEMessage[] = [];
    let cut = this.buffer.indexOf("\n\n");
    while (cut >= 0) {
      const msg = parseBlock(this.buffer.slice(0, cut));
      if (msg) out.push(msg);
      this.buffer = this.buffer.slice(cut + 2);
      cut = this.buffer.indexOf("\n\n");
    }
    return out;
  }
}

function parseBlock(block: string): SSEMessage | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // 注释（心跳）
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}
