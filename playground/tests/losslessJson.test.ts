import { describe, expect, it } from 'vitest';
import { executionSlices, JsonText, serializeSubmitBody } from '../src/utils/losslessJson';

describe('lossless JSON boundary', () => {
  const value = '{"integer":1234567890123456789012345678901234567890,"decimal":1.00000000000000000001,"exponent":1e10000,"ordered":{"2":2,"1":1,"z":3},"escaped":"a\\\"b\\\\c\\n"}';

  it('splices strict input without evaluating numbers or reordering keys', () => {
    const raw = serializeSubmitBody({ entry: 'demo.main', packages: { 'demo.main': { format: 'yaml', text: 'run: {}' } } }, `\n${value}\n`);
    expect(new JsonText(raw).raw(['input'])).toBe(value);
    expect(serializeSubmitBody({ entry: 'x', packages: {} }, ' ')).toContain('"input":{}');
  });

  it('formats only whitespace and indexes nested frame trees by tree path', () => {
    const raw = new JsonText(`{"frames":[{"variables":${value},"children":[{"variables":{"n":1.0},"children":[]}]}],"result":{"output":${value}}}`);
    const slices = executionSlices(raw);
    expect(slices.frameVariablesRaw.get('0.0')).toContain('1.0');
    const formatted = new JsonText(slices.resultOutputRaw!);
    for (const key of ['integer', 'decimal', 'exponent', 'escaped']) expect(formatted.raw([key])).toBe(new JsonText(value).raw([key]));
    expect([...formatted.at(['ordered'])!.entries!.keys()]).toEqual(['2', '1', 'z']);
    expect(raw.raw(['frames', 99, 'variables'])).toBeNull();
  });

  it.each(['', '{', '[1,]', '{"n":01}', '{"n":NaN}', '{"n":Infinity}', '{}{}', '{"a":1,"\\u0061":2}', '{"x":{"a":1,"a":2}}', '{"s":"\\q"}', '{"s":"\n"}'])('rejects malformed or duplicate-key input %s', raw => {
    expect(() => new JsonText(raw)).toThrow();
  });

  it.each(['null', '[]', '1.0', 'true'])('rejects a non-object submission input %s', raw => {
    expect(() => serializeSubmitBody({ entry: 'x', packages: {} }, raw)).toThrow('Input must be a JSON object');
  });
});
