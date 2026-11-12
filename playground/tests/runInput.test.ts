import { readFileSync } from 'node:fs';
import { ref } from 'vue';
import { beforeAll, describe, expect, it } from 'vitest';
import init, { compile_workflow, validate_input, validate_external_input } from '../src/wasm-pkg/axcutor_wasm';
import { useRunInput } from '../src/composables/useRunInput';
import { defaultInput, DEMO_ENTRY, DEMO_INPUT } from '../src/utils/runInput';
import { JsonText } from '../src/utils/losslessJson';

beforeAll(async () => { await init({ module_or_path: readFileSync(new URL('../src/wasm-pkg/axcutor_wasm_bg.wasm', import.meta.url)) }); });

function compileInput(symbols: string): string {
  const text = `{"input":${symbols},"output":{},"run":{"executor":"builtin","content":"return Output()"}}`;
  const json = new JsonText(compile_workflow(JSON.stringify({ entry: '__main__', mounts: {
    __main__: { packages: { __main__: { format: 'json', text } } },
  } })));
  expect(json.raw(['diagnostics'])).toBe('[]');
  return json.raw(['bundle', 'workflows', '__main__', 'input'])!;
}

describe('entry-owned input drafts and real WASM contracts', () => {
  it('builds external handle defaults from lossless locator schemas and refreshes restored drafts when resources change', () => {
    const input = ref('{"f":{"type":"handle","kind":"infra:File"}}');
    const resources = ref('{"infra:File":{"locator":{"type":"object","properties":{"id":{"type":"number","enum":[1.00000000000000000001]}}}}}');
    const state = useRunInput(ref('public.yaml'), input, resources);
    expect(state.inputRaw.value).toBe('{"f":{"$handle":"infra:File","locator":{"id":1.00000000000000000001}}}');
    expect(state.inputError.value).toBeNull();
    expect(() => validate_external_input(state.inputRaw.value, input.value, resources.value)).not.toThrow();
    state.restore(state.inputRaw.value, 'old');
    resources.value = '{"infra:File":{"locator":{"type":"string","enum":["new"]}}}';
    expect(state.inputRaw.value).toBe('{"f":{"$handle":"infra:File","locator":"new"}}');
    state.inputRaw.value = '{"f":{"$handle":"private-rid","kind":"infra:File"}}';
    expect(state.inputError.value).not.toBeNull();
    resources.value = '{}';
    expect(state.inputRaw.value).toContain('private-rid');
    expect(state.inputError.value).not.toBeNull();
  });

  it('preflights output-only root handles and private handles even when inputs are null', () => {
    const resources = ref('{}');
    const state = useRunInput(ref('main'), ref('{"f":{"type":["handle","null"],"kind":"infra:File"}}'), resources);
    expect(state.inputRaw.value).toBe('{"f":null}');
    expect(state.inputError.value).not.toBeNull();
    resources.value = '{"infra:File":{"locator":{"type":"string"}}}';
    expect(state.inputError.value).toBeNull();
    const outputOnly = useRunInput(ref('output'), ref('{}'), ref('{}'), ref('{"f":{"type":"handle","kind":"infra:File"}}'));
    expect(outputOnly.inputError.value).not.toBeNull();
  });

  it('constructs recursive required defaults in declaration order without rounding enum literals', () => {
    const schema = compileInput(`{
      "z":{"type":["integer","null"]},
      "ratio":{"type":"number"},
      "choice":{"type":"integer","enum":[999999999999999999999999999999,2]},
      "fraction":{"type":"number","enum":[1.00000000000000000001,2.0]},
      "text":{"type":"string"},
      "flag":{"type":"boolean"},
      "list":{"type":"array","items":{"type":"string"}},
      "map":{"type":"object","additionalProperties":{"type":"integer"}},
      "record":{"type":["object","null"],"properties":{
        "later":{"type":"string","enum":["first","second"]},
        "skip":{"type":"boolean"},
        "earlier":{"type":"object","properties":{"n":{"type":"number"}}}
      },"required":["earlier","later"]}
    }`);
    const raw = defaultInput(schema);
    expect(raw).toBe('{"z":0,"ratio":0.0,"choice":999999999999999999999999999999,"fraction":1.00000000000000000001,"text":"","flag":false,"list":[],"map":{},"record":{"later":"first","earlier":{"n":0.0}}}');
    expect(() => validate_input(raw, schema)).not.toThrow();
    expect(defaultInput(compileInput('{}'))).toBe('{}');
    expect(defaultInput(compileInput('{"record":{"type":"object","properties":{"optional":{"type":"string"}},"required":[]}}'))).toBe('{"record":{}}');
  });

  it('uses the demo preset only while its contract accepts it', () => {
    const schema = ref<string | null>(compileInput('{"items":{"type":"array","items":{"type":"integer"}},"delay_seconds":{"type":"integer"}}'));
    const state = useRunInput(ref(DEMO_ENTRY), schema);
    expect(state.inputRaw.value).toBe(DEMO_INPUT);
    schema.value = compileInput('{}');
    expect(state.inputRaw.value).toBe('{}');
    schema.value = compileInput('{"other":{"type":"number"}}');
    expect(state.inputRaw.value).toBe('{"other":0.0}');
  });

  it('keeps edited text by physical entry path while untouched defaults follow schema changes', () => {
    const entry = ref('a.json');
    const schema = ref<string | null>(compileInput('{"n":{"type":"integer"}}'));
    const state = useRunInput(entry, schema);
    expect(state.inputRaw.value).toBe('{"n":0}');
    const manual = ' { "n": 999999999999999999999999999 }\n';
    state.inputRaw.value = manual;
    entry.value = 'b.json';
    schema.value = compileInput('{"fraction":{"type":"number"}}');
    expect(state.inputRaw.value).toBe('{"fraction":0.0}');
    schema.value = compileInput('{"flag":{"type":"boolean"}}');
    expect(state.inputRaw.value).toBe('{"flag":false}');
    entry.value = 'a.json';
    expect(state.inputRaw.value).toBe(manual);
  });

  it.each(['null', '[]', '{', '{"n":1.0}', '{"n":1,"n":2}', '{"n":NaN}', '{"n":Infinity}'])('preflights invalid input %s', raw => {
    const state = useRunInput(ref('main.json'), ref(compileInput('{"n":{"type":"integer"}}')));
    state.inputRaw.value = raw;
    expect(state.inputError.value).toMatch(/^Input:/);
  });

  it('validates arbitrary precision inputs against exact numeric enums', () => {
    const schema = compileInput('{"n":{"type":"integer","enum":[999999999999999999999999999999]},"f":{"type":"number","enum":[1.00000000000000000001]}}');
    const state = useRunInput(ref('main.json'), ref(schema));
    expect(state.inputError.value).toBeNull();
    state.inputRaw.value = '{"n":999999999999999999999999999999,"f":1.00000000000000000001}';
    expect(state.inputError.value).toBeNull();
    state.inputRaw.value = '{"n":999999999999999999999999999999,"f":1.0}';
    expect(state.inputError.value).not.toBeNull();
  });

  it('restores an untouched draft once per execution without overwriting manual edits or changed defaults', () => {
    const schema = ref<string | null>(compileInput('{"n":{"type":"integer"}}'));
    const state = useRunInput(ref('main.json'), schema);
    state.restore('{"n":99}', 'previous');
    expect(state.inputRaw.value).toBe('{"n":99}');
    schema.value = compileInput('{"n":{"type":"string"}}');
    state.restore('{"n":99}', 'previous');
    expect(state.inputRaw.value).toBe('{"n":""}');
    state.restore('{"n":"recovered"}', 'next');
    expect(state.inputRaw.value).toBe('{"n":"recovered"}');
    state.inputRaw.value = '{"n":"typed before delayed recovery"}';
    state.restore('{"n":"late response"}', 'late');
    expect(state.inputRaw.value).toBe('{"n":"typed before delayed recovery"}');
  });

  it('defers validation until a compiled schema is available', () => {
    const schema = ref<string | null>(null);
    const state = useRunInput(ref('main.json'), schema);
    state.inputRaw.value = '{';
    expect(state.inputError.value).toBeNull();
    schema.value = compileInput('{}');
    expect(state.inputError.value).not.toBeNull();
  });
});
