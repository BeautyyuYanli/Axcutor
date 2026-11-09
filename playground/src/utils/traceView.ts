import type { TraceSpan } from '../client/types.gen';
import type { WorkflowBundle } from '../types/compiler';
import { locateFrame } from './graphLayout';

export function traceRows(spans: TraceSpan[]): { span: TraceSpan; depth: number }[] {
  const children = new Map<string | null, TraceSpan[]>();
  const ids = new Set(spans.map(span => span.id));
  for (const span of spans) {
    const parent = span.parent_id && ids.has(span.parent_id) ? span.parent_id : null;
    const siblings = children.get(parent) ?? [];
    siblings.push(span);
    children.set(parent, siblings);
  }
  const visit = (parent: string | null, depth: number): { span: TraceSpan; depth: number }[] =>
    (children.get(parent) ?? []).flatMap(span => [{ span, depth }, ...visit(span.id, depth + 1)]);
  return visit(null, 0);
}

export function spanDuration(span: TraceSpan, observedAt: string | null): number | null {
  const finish = span.finished_at ?? observedAt;
  return finish ? Math.max(0, Date.parse(finish) - Date.parse(span.started_at)) : null;
}

export function durationText(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds)) return 'Unknown duration';
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(3)} s`;
}

/** A span points to the caller's CFG node, including completed iterations.
 * Reuse frame address traversal so nested workflows/inline graphs follow the
 * same mapping as live overlays. Root calls point to their root run/start.
 */
export function locateSpan(bundle: WorkflowBundle, span: TraceSpan) {
  return locateFrame(bundle, {
    kind: span.frame_kind, address: span.address, cursor: span.cursor,
    waiting: [], call_depth: 1, steps: 0, max_steps: null, variables: {}, children: [],
  });
}
