import { describe, expect, it } from 'vitest';
import type { Execution, Frame } from '../src/client/types.gen';
import { executionSummary } from '../src/utils/executionView';

const frame: Frame = { kind: 'function', address: 'main', cursor: 'review', waiting: [], children: [], call_depth: 1, steps: 1, max_steps: null, variables: {} };
const running: Execution = { execution_id: 'demo', status: 'running', started_at: '2026-01-01T00:00:00Z', finished_at: null, frames: [frame], result: null, error: null };
const state = { preparing: false, submitting: false, cancelRequested: false, execution: null as Execution | null, observedId: null as string | null, requestProblem: false };

describe('observed execution presentation', () => {
  it('hides a previous success throughout saving and submission', () => {
    const previous = { ...running, status: 'completed' as const, frames: [], result: { branch: null, output: { total: 12 } } };
    for (const flags of [{ preparing: true }, { submitting: true }]) {
      expect(executionSummary({ ...state, execution: previous, ...flags }).label).toBe('Submitting');
    }
  });

  it('treats an unknown ID and failed status reads as observation, not execution failure', () => {
    expect(executionSummary({ ...state, observedId: 'demo' }).label).toBe('Observing');
    for (const execution of [null, running]) {
      const summary = executionSummary({ ...state, execution, observedId: 'demo', requestProblem: true });
      expect(summary.label).toBe('Observing');
      expect(summary.message).toContain('retry');
      expect(summary.message).toContain('cancel');
    }
    expect(executionSummary({ ...state, requestProblem: true }).label).toBe('Submission not confirmed');
  });

  it('keeps cancellation pending through a failed observation and trusts only real terminal states', () => {
    expect(executionSummary({ ...state, execution: running, observedId: 'demo', cancelRequested: true, requestProblem: true }).label).toBe('Cancelling');
    for (const [status, label] of [['completed', 'Succeeded'], ['failed', 'Failed'], ['cancelled', 'Cancelled']] as const) {
      expect(executionSummary({ ...state, execution: { ...running, status, frames: [] } }).label).toBe(label);
    }
  });

  it('recognizes a waiting child while other current steps are still running', () => {
    const execution = { ...running, frames: [{ ...frame, children: [{ ...frame, address: 'main/review', waiting: ['main/review/decision'] }] }] };
    expect(executionSummary({ ...state, execution, observedId: 'demo' }).label).toBe('Waiting for signal');
    expect(executionSummary({ ...state, execution: running, observedId: 'demo' }).label).toBe('Running');
  });
});
