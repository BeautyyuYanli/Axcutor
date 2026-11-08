import main from '../../../examples/workflows/main.yaml?raw';
import double from '../../../examples/workflows/double.yaml?raw';
import input from '../../../examples/inputs/demo.json?raw';
import type { SourceDocument } from '../types/compiler';

/** Fresh editable copies of the repository's runnable source closure. */
export function defaultPreset(): { entry: string; sources: Record<string, SourceDocument>; inputRawText: string } {
  return {
    entry: 'main.yaml',
    sources: { 'main.yaml': { format: 'yaml', text: main }, 'double.yaml': { format: 'yaml', text: double } },
    inputRawText: input.trim(),
  };
}
