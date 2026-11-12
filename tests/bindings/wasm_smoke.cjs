// Run the generated nodejs glue. Each call executes the actual .wasm module.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const api = require(path.resolve(process.argv[2]));
const oracle = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const hostError = error => error instanceof Error && !(error instanceof WebAssembly.RuntimeError);
let queries = 0;
let invalidQueries = 0;
for (const entry of oracle) {
  assert.equal(api.compile_workflow(entry.request_json), entry.result_json);
  for (const query of entry.queries) {
    assert.equal(api.get_available_vars_at_node(query.analysis_json, query.node_id, query.expected_json), query.vars_json);
    assert.equal(api.derive_node_ports(query.analysis_json, query.node_id), query.ports_json);
    queries++;
  }
  for (const query of entry.invalid_queries) {
    assert.throws(() => api.get_available_vars_at_node(query.analysis_json, query.node_id, query.expected_json), hostError);
    if (query.check_ports) assert.throws(() => api.derive_node_ports(query.analysis_json, query.node_id), hostError);
    invalidQueries++;
  }
}
console.log('Rejected query groups:', invalidQueries);
const handleSchema = '{"tx":{"type":"handle","kind":"infra.postgres:Tx"}}';
const handleValue = '{"tx":{"$handle":"res_1","kind":"infra.postgres:Tx"}}';
assert.deepEqual(JSON.parse(api.validate_input(handleValue, handleSchema)), JSON.parse(handleValue));
assert.deepEqual(JSON.parse(api.validate_run_contract('{"output":' + handleValue + '}', handleSchema, '{}')).output, JSON.parse(handleValue));
assert.throws(() => api.validate_root_interface(handleSchema, '{}', '{}'), hostError);
const resources = '{"infra.postgres:Tx":{"locator":{"type":"integer"}}}';
const external = '{"tx":{"$handle":"infra.postgres:Tx","locator":123456789012345678901234567890}}';
assert.equal(api.validate_root_interface(handleSchema, '{}', resources), 'null');
assert.equal(api.validate_external_input(external, handleSchema, resources), external);
assert.equal(api.validate_external_output('{"output":' + external + '}', handleSchema, '{}', resources), '{"branch":null,"output":' + external + '}');
assert.equal(api.validate_literal('123456789012345678901234567890', '{"type":"integer"}'), '123456789012345678901234567890');
assert.equal(api.literal_equal('1', '1.0'), true);
assert.throws(() => api.validate_external_input(handleValue, handleSchema, resources), hostError);
for (const locator of ['{"type":["integer","null"]}', '{"type":"handle","kind":"infra.postgres:Tx"}']) {
  const invalidResources = '{"infra.postgres:Tx":{"locator":' + locator + '}}';
  assert.throws(() => api.validate_root_interface(handleSchema, '{}', invalidResources), hostError);
  assert.throws(() => api.validate_external_input(external, handleSchema, invalidResources), hostError);
  assert.throws(() => api.validate_external_output('{"output":' + external + '}', handleSchema, '{}', invalidResources), hostError);
}
assert.throws(() => api.validate_input('{"tx":{"$handle":"res_1","kind":"infra.postgres:Other"}}', handleSchema), hostError);
assert.throws(() => api.compile_workflow('{}'));
assert.throws(() => api.compile_workflow('not JSON'));
assert.throws(() => api.get_available_vars_at_node('{}', 'n'));
assert.throws(() => api.derive_node_ports('not JSON', 'n'));
const shallow = oracle.find(entry => entry.queries.length).queries[0].analysis_json;
const deepType = '{"type":"array","items":'.repeat(2048) + '{"type":"integer"}' + '}'.repeat(2048);
const deepAnalysis = shallow.slice(0, -1) + ',"extra":' + '['.repeat(2048) + '0' + ']'.repeat(2048) + '}';
assert.throws(() => api.get_available_vars_at_node(shallow, 'n', deepType), hostError);
assert.throws(() => api.get_available_vars_at_node(deepAnalysis, 'n'), hostError);
assert.throws(() => api.derive_node_ports(deepAnalysis, 'n'), hostError);
console.log(`WebAssembly: ${oracle.length} compile cases, ${queries} canvas queries passed`);
