import main from '../../examples/workflows/demo/main/__axcutor__.yaml?raw';
import double from '../../examples/workflows/demo/double/__axcutor__.yaml?raw';
import { DEMO_INPUT } from '../src/utils/runInput';
import type { CompileRequest } from '../src/types/compiler';

/** Fresh editable copies of the repository's runnable source closure. */
export function defaultPreset(): CompileRequest & { inputRawText: string } {
  return {
    entry: 'demo.main',
    mounts: { demo: { packages: { 'demo.main': { format: 'yaml', text: main }, 'demo.double': { format: 'yaml', text: double } } } },
    inputRawText: DEMO_INPUT,
  };
}
