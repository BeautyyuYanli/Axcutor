/** A strict JSON token index. Numbers are never evaluated and object entries
 * retain textual order, including integer-like keys. Only string tokens are
 * decoded, for duplicate detection and path addressing. Formatting changes
 * whitespace outside strings only. The index also protects compiler enum tokens
 * when handing an analysis back to WASM.
 */
export interface JsonToken {
  start: number;
  end: number;
  kind: 'object' | 'array' | 'scalar';
  entries?: Map<string, JsonToken>;
  items?: JsonToken[];
  keys?: Map<string, string>;
}
export type JsonPath = readonly (string | number)[];

export class JsonText {
  readonly root: JsonToken;
  constructor(readonly text: string) {
    let offset = 0;
    const whitespace = () => { while (/[\x20\t\r\n]/.test(text[offset] ?? '\0')) offset++; };
    const fail = (message: string): never => { throw new SyntaxError(`${message} at offset ${offset}`); };
    const string = (): string => {
      const start = offset++;
      while (offset < text.length) {
        const c = text[offset++];
        if (c === '"') {
          const token = text.slice(start, offset);
          JSON.parse(token); // String decoding cannot lose numbers or key order.
          return token;
        }
        if (c === '\\') offset++;
      }
      return fail('Unterminated string');
    };
    const value = (depth: number): JsonToken => {
      if (depth > 512) fail('JSON nesting limit exceeded');
      whitespace();
      const start = offset;
      const c = text[offset];
      if (c === '{' || c === '[') {
        offset++;
        const token: JsonToken = c === '{'
          ? { start, end: 0, kind: 'object', entries: new Map(), keys: new Map() }
          : { start, end: 0, kind: 'array', items: [] };
        const close = c === '{' ? '}' : ']';
        whitespace();
        if (text[offset] !== close) {
          while (true) {
            whitespace();
            if (c === '{') {
              if (text[offset] !== '"') fail('Expected object key');
              const keyToken = string();
              const key: string = JSON.parse(keyToken);
              if (token.entries!.has(key)) fail(`Duplicate object key ${keyToken}`);
              whitespace();
              if (text[offset++] !== ':') fail('Expected colon');
              token.entries!.set(key, value(depth + 1));
              token.keys!.set(key, keyToken);
            } else token.items!.push(value(depth + 1));
            whitespace();
            if (text[offset] !== ',') break;
            offset++;
          }
        }
        if (text[offset++] !== close) fail(`Expected ${close}`);
        token.end = offset;
        return token;
      }
      if (c === '"') string();
      else {
        const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(offset));
        if (!match) fail('Expected JSON value');
        offset += match![0].length;
      }
      return { start, end: offset, kind: 'scalar' };
    };
    this.root = value(0);
    whitespace();
    if (offset !== text.length) fail('Unexpected trailing content');
  }

  at(path: JsonPath): JsonToken | undefined {
    let token: JsonToken | undefined = this.root;
    for (const key of path) token = typeof key === 'number' ? token?.items?.[key] : token?.entries?.get(key);
    return token;
  }

  raw(path: JsonPath = []): string | null {
    const token = this.at(path);
    return token ? this.text.slice(token.start, token.end) : null;
  }

  format(path: JsonPath = []): string | null {
    const token = this.at(path);
    if (!token) return null;
    const render = (node: JsonToken, depth: number): string => {
      if (node.kind === 'scalar') return this.text.slice(node.start, node.end);
      const object = node.kind === 'object';
      const values = object
        ? [...node.entries!].map(([key, child]) => `${node.keys!.get(key)}: ${render(child, depth + 1)}`)
        : node.items!.map(child => render(child, depth + 1));
      const [open, close] = object ? ['{', '}'] : ['[', ']'];
      if (!values.length) return open + close;
      return `${open}\n${'  '.repeat(depth + 1)}${values.join(`,\n${'  '.repeat(depth + 1)}`)}\n${'  '.repeat(depth)}${close}`;
    };
    return render(token, 0);
  }
}

export interface RawExecutionSlices {
  /** Tree index paths avoid collisions between iteration and function addresses. */
  frameVariablesRaw: Map<string, string>;
  resultOutputRaw: string | null;
}

export function executionSlices(json: JsonText): RawExecutionSlices {
  const frameVariablesRaw = new Map<string, string>();
  const visit = (path: (string | number)[], key: string) => {
    const frames = json.at(path)?.items ?? [];
    frames.forEach((_, i) => {
      const framePath = [...path, i];
      const frameKey = key ? `${key}.${i}` : String(i);
      frameVariablesRaw.set(frameKey, json.format([...framePath, 'variables']) ?? '{}');
      visit([...framePath, 'children'], frameKey);
    });
  };
  visit(['frames'], '');
  return { frameVariablesRaw, resultOutputRaw: json.format(['result', 'output']) };
}
