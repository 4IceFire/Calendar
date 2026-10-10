const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

test('Props history text distinguishes saved-library commands from numbered presets', () => {
  const script = fs.readFileSync('static/props.js', 'utf8')
  const start = script.indexOf('    function catalogStatus() {')
  const end = script.indexOf('    function plainLibrary()', start)
  const last = {textContent:''}
  const state = {catalog:{}, last_triggered:{kind:'library',position:null,name:'Welcome'}}
  const context = {state, status:{}, document:{getElementById:() => last}}
  vm.runInNewContext(script.slice(start,end) + '\ncatalogStatus();', context)
  assert.match(last.textContent, /Library: Welcome/)
  assert.doesNotMatch(last.textContent, /null/)
  state.last_triggered = {position:2,name:'Welcome'}
  vm.runInNewContext('catalogStatus();',context)
  assert.match(last.textContent, /2\. Welcome/)
})
