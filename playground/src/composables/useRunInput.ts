/** Entry paths own in-memory drafts independently of workspace files/revisions.
 * Untouched defaults follow compilation; editing claims the draft permanently
 * for this page. Late trace recovery may only replace an unedited draft.
 */
import { computed, ref, type Ref } from 'vue';
import { validate_input } from '../wasm-pkg/axcutor_wasm';
import { defaultInput, DEMO_ENTRY, DEMO_INPUT } from '../utils/runInput';

export function useRunInput(entryPath: Ref<string>, schemaRaw: Ref<string | null>) {
  const drafts = ref(new Map<string, { text: string; edited: boolean; schema: string | null; executionId?: string }>());
  const inputRaw = computed({
    get() {
      const draft = drafts.value.get(entryPath.value);
      if (draft && (draft.edited || draft.schema === schemaRaw.value)) return draft.text;
      if (schemaRaw.value === null) return '{}';
      if (entryPath.value === DEMO_ENTRY) {
        try { validate_input(DEMO_INPUT, schemaRaw.value); return DEMO_INPUT; }
        catch { /* A changed demo contract uses the same skeleton as other entries. */ }
      }
      return defaultInput(schemaRaw.value);
    },
    set(text: string) { drafts.value.set(entryPath.value, { text, edited: true, schema: schemaRaw.value }); },
  });
  const inputError = computed(() => {
    if (schemaRaw.value === null) return null;
    try { validate_input(inputRaw.value, schemaRaw.value); return null; }
    catch (cause) { return `Input: ${String(cause)}`; }
  });

  function restore(raw: string, executionId: string) {
    const draft = drafts.value.get(entryPath.value);
    if (!draft?.edited && draft?.executionId !== executionId) {
      drafts.value.set(entryPath.value, { text: raw, edited: false, schema: schemaRaw.value, executionId });
    }
  }

  return { inputRaw, inputError, restore };
}
