import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply } from '../index.js'

const directory = mkdtempSync(join(tmpdir(), 'jp-vertical-plugin-'))
const script = join(directory, 'stub.py')
writeFileSync(script, `import json, sys, time, subprocess, pathlib
r=json.load(sys.stdin)
if r['args'].get('workspace')=='cancel':
    c=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    pathlib.Path(__file__).with_suffix('.pid').write_text(str(c.pid))
    time.sleep(30)
elif r['args'].get('workspace')=='invalid':
    print('not json')
elif r['args'].get('workspace')=='blocked':
    print(json.dumps({'ok':False,'status':'blocked','issues':['review required']}));sys.exit(2)
else:
    print(json.dumps({'ok':True,'status':'passed','request':r,'evidence':{'count':0}},ensure_ascii=False))
`, 'utf8')
const registry = new Map(), sections = []
try {
  await apply({ tools: { register: tool => registry.set(tool.name, tool) }, systemPrompt: { section: value => sections.push(value) } }, {
    python: process.env.DSH_JP_VERTICAL_PYTHON || 'python', script,
  })
  assert.deepEqual([...registry.keys()], ['jp_vertical_prepare', 'jp_vertical_status', 'jp_vertical_run'])
  assert.equal(sections.length, 1)
  for (const tool of registry.values()) {
    assert.equal(tool.output.schema.type, 'object')
    assert.equal(tool.output.schema.additionalProperties, true)
    const value = await tool.execute({ source: '日語.pdf', workspace: '工作区', title: '書名', author: '作者', stage: 'translate', pages: [2, 17] }, {})
    assert.deepEqual(JSON.parse(JSON.stringify(value)), value)
    assert.equal(value.request.command, tool.name.replace('jp_vertical_', ''))
    assert.equal(tool.output.render({}, value)[0].type, 'text')
    if (value.request.command === 'run') assert.deepEqual(value.request.args.pages, [2, 17])
    else assert.equal(value.request.args.pages, undefined)
  }
  const status = registry.get('jp_vertical_status')
  assert.equal((await status.execute({ workspace: 'blocked' }, {})).status, 'blocked')
  await assert.rejects(status.execute({ workspace: 'invalid' }, {}), /jp-vertical-kb/)
  await assert.rejects(registry.get('jp_vertical_run').execute({ workspace: 'x', stage: 'ocr', timeout_seconds: 3601 }, {}), /timeout_seconds/)
  const pre = new AbortController(); pre.abort()
  await assert.rejects(status.execute({ workspace: 'x' }, { signal: pre.signal }), /cancelled before launch/)
  const controller = new AbortController()
  const pending = status.execute({ workspace: 'cancel' }, { signal: controller.signal })
  const outcome = assert.rejects(pending, /cancelled/)
  const deadline = Date.now() + 5000
  while (!existsSync(join(directory, 'stub.pid')) && Date.now() < deadline) await new Promise(done => setTimeout(done, 25))
  assert.ok(existsSync(join(directory, 'stub.pid')), 'stub child must start')
  const pid = Number(readFileSync(join(directory, 'stub.pid'), 'utf8'))
  controller.abort()
  await outcome
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' }, 'owned grandchild must be terminated')
  console.log('PASS: registration, schemas, JSON, exact pages, input whitelist, failed gateway, cancellation and process-tree cleanup')
} finally { rmSync(directory, { recursive: true, force: true }) }
