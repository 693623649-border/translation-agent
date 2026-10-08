// Pure contract validation: rejected inputs never start a Python process.
import assert from 'node:assert/strict'
import { apply, default as plugin } from '../index.js'
const registered = new Map()
await apply({tools: {register: d => registered.set(d.name, d)}, systemPrompt: {section() {}}}, {})
const ask = registered.get('kb_ask')
assert.match(plugin.CONTRACT, /hybrid/)
for (const args of [{semantic: false}, {deep: false}, {mode: 'lexical'}, {scope: 'pages'}]) {
  await assert.rejects(ask.execute({query: 'test', ...args}, {}), /requires hybrid|must be one of/)
}
console.log('hybrid policy: 4 forbidden requests rejected without subprocess/network')

