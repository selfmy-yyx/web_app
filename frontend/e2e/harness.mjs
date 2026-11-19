import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const root = fileURLToPath(new URL('../../', import.meta.url))
const origin = 'http://localhost:5173'

export function backend(test) {
  const child = spawn(process.env.PANEL_E2E_PYTHON || 'python', ['frontend/e2e/bridge.py'], {
    cwd: root,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(process.platform === 'win32' ? ';' : ':') },
    stdio: ['pipe', 'pipe', 'inherit'],
  })
  const pending = new Map()
  const exit = once(child, 'exit')
  exit.catch(() => {})
  let id = 0
  let failed
  function rejectPending(error) {
    failed = error
    for (const item of pending.values()) item.reject(error)
    pending.clear()
  }
  child.on('error', rejectPending)
  child.on('exit', code => rejectPending(new Error(`ASGI bridge exited with code ${code}`)))
  child.stdin.on('error', rejectPending)
  createInterface({ input: child.stdout }).on('line', line => {
    const reply = JSON.parse(line)
    const item = pending.get(reply.id)
    if (reply.error) item?.reject(new Error(reply.error))
    else item?.resolve(reply)
    pending.delete(reply.id)
  })

  test.after(async () => {
    child.stdin.end(JSON.stringify({ op: 'stop' }) + '\n')
    const stop = setTimeout(() => child.kill(), 5000)
    try {
      await exit
    } finally {
      clearTimeout(stop)
    }
  })

  function send(command) {
    if (failed) throw failed
    const key = ++id
    return new Promise((resolve, reject) => {
      pending.set(key, { resolve, reject })
      child.stdin.write(JSON.stringify({ ...command, id: key }) + '\n')
    })
  }

  return {
    async route(context) {
      await context.route(origin + '/**', async route => {
        const request = route.request()
        const url = new URL(request.url())
        if (!url.pathname.startsWith('/api/')) {
          const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1)
          await route.fulfill({ path: join(root, 'frontend', 'dist', name) })
          return
        }
        const { status, headers, body } = await send({
          op: 'request', method: request.method(), path: url.pathname,
          query: url.search.slice(1), headers: await request.allHeaders(), body: request.postData(),
        })
        await route.fulfill({ status, headers, body })
      })
    },
    release: (topic, turn) => send({ op: 'release', topic, turn }),
    origin,
  }
}
