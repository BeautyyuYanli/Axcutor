/** Construct input literals from the checked WASM Symbols text. Only schema
 * strings are decoded; enum numbers and declaration order stay as JSON tokens.
 */
import { JsonText, type JsonPath } from './losslessJson';

export const DEMO_ENTRY = 'workflows/demo/main/__axcutor__.yaml';
export const DEMO_INPUT = '{"items":[3,1,2],"delay_seconds":5}';

export function defaultInput(schemaRaw: string, resourcesRaw = '{}'): string {
  const schema = new JsonText(schemaRaw);
  const resources = new JsonText(resourcesRaw);
  const literal = (path: JsonPath, source = schema): string => {
    const enumeration = source.raw([...path, 'enum', 0]);
    if (enumeration !== null) return enumeration;
    const types = JSON.parse(source.raw([...path, 'type'])!) as string | string[];
    const type = Array.isArray(types) ? types.find(type => type !== 'null') : types;
    switch (type) {
      case 'integer': return '0';
      case 'number': return '0.0';
      case 'string': return '""';
      case 'boolean': return 'false';
      case 'array': return '[]';
      case 'handle': {
        const kind = JSON.parse(source.raw([...path, 'kind'])!) as string;
        if (resources.raw([kind, 'locator']) === null) return 'null';
        return `{"$handle":${JSON.stringify(kind)},"locator":${literal([kind, 'locator'], resources)}}`;
      }
      case 'object': {
        const properties = source.at([...path, 'properties'])?.entries;
        if (!properties) return '{}';
        const requiredRaw = source.raw([...path, 'required']);
        const required = new Set<string>(requiredRaw === null ? properties.keys() : JSON.parse(requiredRaw));
        return `{${[...properties.keys()].filter(key => required.has(key))
          .map(key => `${JSON.stringify(key)}:${literal([...path, 'properties', key], source)}`).join(',')}}`;
      }
      default: return 'null';
    }
  };
  return `{${[...schema.root.entries!.keys()].map(key => `${JSON.stringify(key)}:${literal([key])}`).join(',')}}`;
}
