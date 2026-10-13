const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/api_tokens.js','utf8');
const start = source.indexOf('    function splitValues(');
const end = source.indexOf('    function openPermissions(', start);
function payload(presets,outputs){
 const values={'api-token-name':'Fixture','api-token-description':'','api-token-expiry':'365','api-token-allowed-paths':'','api-token-tv-targets':'','api-token-vh-outputs':'','api-token-vh-inputs':'','api-token-vh-presets':'','api-token-atem-sources':'','api-token-routing-presets':presets,'api-token-routing-outputs':outputs};
 const context={document:{getElementById:id=>({value:values[id]})},createForm:{querySelectorAll:()=>[{value:'routing_presets'}]}};vm.createContext(context);vm.runInContext(source.slice(start,end),context);return JSON.parse(JSON.stringify(context.creationPayload()));
}
test('routing constraints are exact IDs/ports and blank fields are omitted',()=>{
 const p=payload('a'.repeat(32),'26, 27');assert.deepEqual(p.constraints.routing_presets,['a'.repeat(32)]);assert.deepEqual(p.constraints.routing_outputs,[26,27]);
 assert.equal('routing_presets' in payload('','').constraints,false);
 assert.throws(()=>payload('all','26'));
 assert.throws(()=>payload('a'.repeat(32),'all'));
});
