const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

class Element {
  children = []
  value = ''
  constructor(id) { this.id = id }
  set textContent(value) { this.children = [] }
  appendChild(value) { this.children.push(value) }
}
function selectors(state) {
  const source = fs.readFileSync('static/props.js','utf8')
  const start = source.indexOf('    function sortedChoices(') >= 0 ? source.indexOf('    function sortedChoices(') : source.indexOf('    function choices(')
  const end = source.indexOf('    function renderOrder()',start)
  const context = {state, document:{createElement:() => new Element()}, Option:function(name,value) {this.name=name;this.value=value}}
  vm.createContext(context); vm.runInContext(source.slice(start,end),context)
  return context
}

test('ProPresenter Add/edit target choices sort displayed names case-insensitively with deterministic ties and retain chosen UUID spelling', () => {
  const entries = [{uuid:'z',name:'zebra'}, {uuid:'b',name:'beta'}, {uuid:'a2',name:'Alpha'}, {uuid:'a1',name:'Alpha'}, {uuid:'a3',name:'alpha'}]
  const before = JSON.stringify(entries); const c = selectors({})
  const select = new Element('props-prop'); c.choices(select,entries,'A2','Choose a prop')
  assert.deepEqual(select.children.map(o => o.name), ['Choose a prop','Alpha','Alpha','alpha','beta','zebra'])
  assert.deepEqual(select.children.map(o => o.value), ['','a1','a2','a3','b','z'])
  assert.equal(select.value,'a2'); assert.equal(JSON.stringify(entries),before)
  const edit = new Element(); c.choices(edit,entries,'missing')
  assert.equal(edit.children.at(-1).name,'Unavailable saved target'); assert.equal(edit.value,'missing')
})

test('Add Preset/change-slot choices sort inside each existing optgroup without rearranging folders storage or running order', () => {
  const state = {library:[{id:'z',name:'Zebra'},{id:'b',name:'beta'},{id:'a',name:'Alpha'},{id:'y',name:'Yellow'},{id:'d',name:'delta'}],
    organization:{folders:[{id:'second',name:'Second'},{id:'first',name:'First'}],items:[{id:'z',folderId:null},{id:'a',folderId:null},{id:'b',folderId:'second'},{id:'y',folderId:'first'},{id:'d',folderId:'first'}]},
    order:['y','z','b'],presets:[{position:1,id:'y'},{position:2,id:'z'},{position:3,id:'b'}]}
  const before = JSON.stringify(state); const c = selectors(state)
  const select = new Element('props-selector'); c.presetChoices(select,'y')
  assert.equal(select.children[0].name,'Choose a preset')
  assert.deepEqual(select.children.slice(1).map(g => g.label), ['No folder','Second','First'])
  assert.deepEqual(select.children.slice(1).map(g => g.children.map(o => o.name)), [['Alpha','Zebra'],['beta'],['delta','Yellow']])
  assert.equal(select.value,'y'); assert.equal(JSON.stringify(state),before)
  const slot = new Element(); c.presetChoices(slot,'z'); assert.equal(slot.value,'z')
  assert.deepEqual(slot.children[0].children.map(o => o.value),['a','z'])
})
