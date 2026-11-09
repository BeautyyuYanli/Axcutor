/** Presentation of observed state only: a request error cannot prove execution
 * failure. Step identity survives polls; tree paths address the current raw
 * variables even when siblings disappear or move.
 */
import type { Execution, Frame } from '../client/types.gen';
import type { WorkflowBundle } from '../types/compiler';
import { locateFrame } from './graphLayout';

export interface ExecutionStep {
  frame: Frame;
  key: string;
  path: string;
  label: string;
  children: ExecutionStep[];
}

export function executionSteps(frames: Frame[], bundle: WorkflowBundle | null, prefix = ''): ExecutionStep[] {
  return frames.map((frame, index) => {
    const path = prefix ? `${prefix}.${index}` : String(index);
    const location = bundle && locateFrame(bundle, frame);
    const name = location
      ? [location.workflowId, ...location.scopeGraphPath, location.nodeId].filter(Boolean).join(' › ')
      : [frame.address.replaceAll('/', ' › '), frame.cursor].filter(Boolean).join(' › ');
    const iterations = [...frame.address.matchAll(/\[(\d+)\]/g)].map(match => match[0]);
    return {
      frame, path, key: `${frame.kind}:${frame.address}`,
      label: name + (location && iterations.length ? ` · item ${iterations.join(' / ')}` : ''),
      children: executionSteps(frame.children, bundle, path),
    };
  });
}

export function flattenSteps(steps: ExecutionStep[]): ExecutionStep[] {
  return steps.flatMap(step => [step, ...flattenSteps(step.children)]);
}

function hasWaitingFrame(frames: Frame[]): boolean {
  return frames.some(frame => frame.waiting.length > 0 || hasWaitingFrame(frame.children));
}

export function executionSummary(state: {
  preparing: boolean; submitting: boolean; cancelRequested: boolean;
  execution: Execution | null; observedId: string | null; requestProblem: boolean;
}): { label: string; message: string } {
  if (state.preparing || state.submitting) return { label: 'Submitting', message: 'Saving files and submitting this run…' };
  if (state.cancelRequested) return {
    label: 'Cancelling', message: state.requestProblem
      ? 'Cancellation requested. Status unavailable; observation will retry.'
      : 'Cancellation requested. Waiting for execution and cleanup.',
  };
  if (state.observedId && (state.requestProblem || !state.execution)) return {
    label: 'Observing', message: state.requestProblem
      ? 'Status unavailable; observation will retry. You can still cancel this execution.'
      : 'Execution ID received. Waiting for execution status…',
  };
  const execution = state.execution;
  if (execution?.status === 'running') {
    return hasWaitingFrame(execution.frames)
      ? { label: 'Waiting for signal', message: 'A current step is waiting for an external signal.' }
      : { label: 'Running', message: execution.frames.length ? 'Showing the current steps.' : 'Waiting for current steps…' };
  }
  if (execution?.status === 'completed') return { label: 'Succeeded', message: 'Execution completed. Output is available in Execution.' };
  if (execution?.status === 'failed') return { label: 'Failed', message: 'Execution failed. Open Execution for the failure reason.' };
  if (execution?.status === 'cancelled') return { label: 'Cancelled', message: 'Execution was cancelled.' };
  if (state.requestProblem) return { label: 'Submission not confirmed', message: 'No execution ID received. See Diagnostics for the request error.' };
  return { label: 'Not run', message: 'Run a compiled workflow to view its current steps and output.' };
}
