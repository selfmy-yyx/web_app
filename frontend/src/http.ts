import { eventTypes, type Accepted, type Card, type Confirmed, type DiscussionApi, type LiveEvent, type Snapshot } from './contract'

const base = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(base + path, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    const result = await response.json().catch(() => null)
    throw new Error(result?.error?.message ?? '服务暂时不可用，请重试。')
  }
  return response.json() as Promise<T>
}

export const httpApi: DiscussionApi = {
  list: () => request<{ items: Card[] }>('/discussions'),
  create: draft => request<Accepted>('/discussions', draft),
  regenerate: (id, version) => request<Accepted>(`/discussions/${encodeURIComponent(id)}/roster/regenerate`, { roster_version: version }),
  confirm: (id, version) => request<Confirmed>(`/discussions/${encodeURIComponent(id)}/confirm`, { roster_version: version }),
  detail: id => request<Snapshot>(`/discussions/${encodeURIComponent(id)}`),
  subscribe(id, after, onEvent, onConnection) {
    const url = `${base}/discussions/${encodeURIComponent(id)}/events`
    let applied = after
    let failures = 0
    let stopped = false
    let current: EventSource | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    function open() {
      const connection = new EventSource(`${url}?after=${applied}`)
      current = connection
      connection.onopen = () => { failures = 0; onConnection('open') }
      connection.onerror = () => {
        if (stopped || current !== connection) return
        const fatal = connection.readyState === EventSource.CLOSED
        connection.close()
        current = undefined
        if (fatal || ++failures >= 3) {
          onConnection('reset')
        } else {
          onConnection('retrying')
          timer = setTimeout(open, 500)
        }
      }
      for (const type of eventTypes) {
        connection.addEventListener(type, (event: MessageEvent<string>) => {
          if (stopped || current !== connection) return
          try {
            const cursor = onEvent({ id: Number(event.lastEventId), type, data: JSON.parse(event.data) } as LiveEvent)
            if (cursor !== null) applied = cursor
          } catch {
            connection.close()
            onConnection('reset')
          }
        })
      }
    }
    open()
    return () => {
      stopped = true
      clearTimeout(timer)
      current?.close()
    }
  },
}
