import main from '../../examples/workflows/demo/main/__axcutor__.yaml?raw';
import double from '../../examples/workflows/demo/double/__axcutor__.yaml?raw';
import input from '../../examples/inputs/demo.json?raw';
import type { SourceDocument } from '../src/types/compiler';

/** Fresh editable copies of the repository's runnable source closure. */
export function defaultPreset(): { entry: string; packages: Record<string, SourceDocument>; inputRawText: string } {
  return {
    entry: 'demo.main',
    packages: { 'demo.main': { format: 'yaml', text: main }, 'demo.double': { format: 'yaml', text: double } },
    inputRawText: input.trim(),
  };
}
