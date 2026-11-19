import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { applyEvent, isTerminal, type Card, type Draft, type Failure, type Participant, type Snapshot, type Status } from './contract'
import { httpApi } from './http'
import { mockApi } from './mock'
import './style.css'

const mockMode = import.meta.env.VITE_DATA_MODE === 'mock'
const api = mockMode ? mockApi : httpApi
const statusLabels: Record<Status, string> = {
  generating: '生成阵容中', awaiting_confirmation: '等待确认', running: '进行中', finished: '已结束', failed: '失败',
}
const expertLabels = { idle: '待机', preparing: '准备发言', speaking: '发言中' }
const expertStatusDisplayMs = 180
const operationLabels = {
  roster: '生成阵容', intentions: '收集发言意愿', utterance: '生成发言',
  findings: '提炼共识与分歧', summary: '生成主持人总结',
}

function failureMessage(failure: Failure) {
  const title = failure.stage === 'roster' ? '阵容生成失败，请重试。' : '讨论已中止。'
  const operation = failure.operation && operationLabels[failure.operation]
  const timeout = failure.code === 'MODEL_TIMEOUT' || failure.code === 'ROSTER_TIMEOUT'
  const detail = failure.message ?? (timeout ? '等待模型服务响应超时。' : '模型服务处理失败。')
  return [title, operation && `失败环节：${operation}。`, detail].filter(Boolean).join(' ')
}
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
const currentRoom = () => /^#\/discussions\/([^/]+)$/.exec(location.hash)?.[1] ?? null
const clock = (date: string) => new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(date))
const created = (date: string) => new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(date))

function useDiscussion(id: string) {
  const [state, setState] = useState<Snapshot | null>(null)
  const lastApplied = useRef<Snapshot | null>(null)
  const [error, setError] = useState('')
  const [connection, setConnection] = useState<'open' | 'retrying' | 'offline'>('open')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    let disconnect: (() => void) | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let recoveries = 0
    let displayTimer: ReturnType<typeof setTimeout> | undefined
    const pendingDisplay: { snapshot: Snapshot; hold: boolean }[] = []

    function clearDisplay() {
      clearTimeout(displayTimer)
      displayTimer = undefined
      pendingDisplay.length = 0
    }

    function displayNext() {
      displayTimer = undefined
      while (active && pendingDisplay.length) {
        const next = pendingDisplay.shift()!
        setState(next.snapshot)
        if (next.hold) {
          displayTimer = setTimeout(displayNext, expertStatusDisplayMs)
          return
        }
      }
    }

    function recover() {
      disconnect?.()
      if (!active || timer) return
      if (++recoveries > 3) {
        setConnection('offline')
        return
      }
      setConnection('retrying')
      timer = setTimeout(() => { timer = undefined; void connect(true) }, 700)
    }

    async function connect(resnapshot = false) {
      try {
        let snapshot = (!resnapshot && lastApplied.current) || await api.detail(id)
        if (!active) return
        clearDisplay()
        lastApplied.current = snapshot
        setState(snapshot)
        setError('')
        if (isTerminal(snapshot)) { setConnection('open'); return }
        disconnect = api.subscribe(id, snapshot.event_cursor, event => {
          if (!active || timer) return null
          try {
            const next = applyEvent(snapshot, event)
            if (next === snapshot) return snapshot.event_cursor
            snapshot = next
            lastApplied.current = snapshot
            // Keep the resume cursor current even when a burst needs paced rendering.
            const hold = event.type === 'participant.status'
            pendingDisplay.push({ snapshot, hold })
            if (displayTimer === undefined) displayNext()
            recoveries = 0
            if (isTerminal(snapshot)) disconnect?.()
            return snapshot.event_cursor
          } catch {
            recover()
            return null
          }
        }, status => {
          if (!active) return
          if (status === 'reset') recover()
          else setConnection(status)
        })
      } catch (cause) {
        if (active) {
          setError(message(cause))
          setConnection('offline')
        }
      }
    }
    void connect()
    return () => { active = false; disconnect?.(); clearTimeout(timer); clearDisplay() }
  }, [id, revision])
  function refresh(resnapshot = false) {
    if (resnapshot) lastApplied.current = null
    setError('')
    setConnection('retrying')
    setRevision(value => value + 1)
  }
  return { state, error, connection, refresh }
}

function Mark() {
  return <span className="mark" aria-hidden="true"><i /><i /><i /><i /></span>
}

const themes = {
  paper: '编辑部',
  live: '深夜演播厅',
  work: '产品工作台',
  salon: '圆桌沙龙',
} as const

type Theme = keyof typeof themes

function Header({ room, theme, onThemeChange }: {
  room: boolean
  theme: Theme
  onThemeChange: (theme: Theme) => void
}) {
  return <header className="topbar">
    <a className="identity" href="#/" aria-label="AI Panel Studio 首页"><Mark /><span><strong>AI Panel Studio</strong><small>圆桌讨论工作室</small></span></a>
    <nav aria-label="主导航">
      <select className="theme-select" aria-label="界面主题" value={theme} onChange={event => onThemeChange(event.target.value as Theme)}>
        {Object.entries(themes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      {room && <a className="back" href="#/">← 返回首页</a>}
      {mockMode && <span className="mode"><i /> 模拟演示</span>}
    </nav>
  </header>
}

function Tag({ status }: { status: Status }) {
  return <span className={`tag ${status}`}><i />{statusLabels[status]}</span>
}
function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <p className={error ? 'notice error' : 'notice'} role={error ? 'alert' : 'status'}>{children}</p>
}
function Loading({ children }: { children: ReactNode }) {
  return <div className="empty loading" role="status"><span className="spinner" aria-hidden="true" />{children}</div>
}

function Home({ draft, change }: { draft: Draft; change: (draft: Draft) => void }) {
  const [cards, setCards] = useState<Card[] | null>(null)
  const [listError, setListError] = useState('')
  const [pending, setPending] = useState(false)
  const [formError, setFormError] = useState('')
  useEffect(() => {
    let active = true
    let revision = 0
    async function load() {
      const attempt = ++revision
      try {
        const { items } = await api.list()
        if (active && attempt === revision) { setCards(items); setListError('') }
      } catch (cause) { if (active && attempt === revision) setListError(message(cause)) }
    }
    function visibleLoad() { if (!document.hidden) void load() }
    void load()
    const timer = setInterval(visibleLoad, 5000)
    window.addEventListener('focus', visibleLoad)
    return () => { active = false; clearInterval(timer); window.removeEventListener('focus', visibleLoad) }
  }, [])
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const topic = draft.topic.trim()
    if (!topic || Array.from(topic).length > 200) { setFormError('请输入 1–200 个字符的话题。'); return }
    setPending(true); setFormError('')
    try {
      const result = await api.create({ ...draft, topic })
      location.hash = `/discussions/${result.id}`
    } catch (cause) { setFormError(message(cause)); setPending(false) }
  }

  return <main className="home" aria-label="工作台">
    <section className="hero">
      <div><h1>AI 圆桌演播厅<span>.</span></h1><p>围绕一个话题，观看多位 AI 专家实时交锋。</p></div>
    </section>
    <div className="home-grid">
      <section className="panel create">
        <div className="panel-heading"><span className="section-icon" aria-hidden="true">＋</span><h2>发起新讨论</h2></div>
        <form className="panel-body compose" onSubmit={submit}>
          <div className="field"><label htmlFor="topic">讨论话题</label><textarea id="topic" placeholder="例如：混合办公能否成为团队的长期默认？" maxLength={400} value={draft.topic} onChange={event => change({ ...draft, topic: event.target.value })} /><small className="counter">{Array.from(draft.topic.trim()).length} / 200</small></div>
          <div className="field"><label htmlFor="expert-count">专家人数</label><div className="count-row"><select id="expert-count" value={draft.expert_count} onChange={event => change({ ...draft, expert_count: Number(event.target.value) })}>{[2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n} 位专家</option>)}</select><small>另设 1 位主持人</small></div></div>
          {formError && <Notice error>{formError}</Notice>}
          <button className="button primary create-button" disabled={pending}>{pending ? '正在创建讨论…' : '生成阵容'}<span aria-hidden="true">→</span></button>
        </form>
      </section>
      <section className="panel sessions">
        <div className="panel-heading"><span className="section-icon" aria-hidden="true">◉</span><h2>进行中的讨论</h2>{cards && <span className="total">{cards.length} 场</span>}</div>
        <div className="panel-body session-list">
          {listError && <Notice error>{listError}</Notice>}
          {!cards && !listError && <Loading>正在加载讨论…</Loading>}
          {cards?.length === 0 && <div className="empty"><span aria-hidden="true">◌</span>暂无进行中的讨论</div>}
          {cards?.map(card => <article className="session" key={card.id}>
            <Tag status={card.status} /><h3>{card.topic}</h3>
            <div className="session-bottom"><small>{card.expert_count} 位专家 · {created(card.started_at)} 开场</small><a href={`#/discussions/${card.id}`}>进入演播厅 <span aria-hidden="true">↗</span></a></div>
          </article>)}
        </div>
      </section>
    </div>
  </main>
}

function Person({ person, live = false }: { person: Participant; live?: boolean }) {
  const status = person.public_status
  return <article className="person" style={{ '--person-color': person.color } as CSSProperties}>
    <div className="person-top"><span className="avatar" aria-hidden="true">{Array.from(person.name)[0]}</span>
      <div className="person-info"><h3>{person.name}</h3><p>{person.title}</p></div>
      {live && status && <span className={`expert-state ${status}`}><i />{expertLabels[status]}</span>}
    </div>
    {live && status ? <p className="person-copy">{person.public_focus || '暂无公开关注点'}</p> : <p className="person-copy">{person.stance}</p>}
  </article>
}

function Roster({ state, refresh, edit }: { state: Snapshot; refresh: () => void; edit: (draft: Draft) => void }) {
  const [pending, setPending] = useState<'confirm' | 'regenerate' | null>(null)
  const [error, setError] = useState('')
  const host = state.participants.find(person => person.role === 'host')
  const experts = state.participants.filter(person => person.role === 'expert')
  async function submit(action: 'confirm' | 'regenerate') {
    setPending(action); setError('')
    try {
      if (action === 'confirm') await api.confirm(state.id, state.roster_version)
      else await api.regenerate(state.id, state.roster_version)
      refresh()
    } catch (cause) { setError(message(cause)) }
    finally { setPending(null) }
  }
  return <section className="panel roster">
    <div className="panel-heading"><span className="section-icon" aria-hidden="true">◎</span><h2>阵容确认</h2><span className="total">1 位主持人 · {state.expert_count} 位专家</span></div>
    <div className="panel-body roster-content">
      {state.status === 'generating' && <Loading>正在生成阵容…</Loading>}
      {state.failure?.stage === 'roster' && <Notice error>{failureMessage(state.failure)}</Notice>}
      {host && <section className="roster-group"><h3>主持人</h3><div className="person-grid"><Person person={host} /></div></section>}
      {experts.length > 0 && <section className="roster-group"><h3>专家</h3><div className="person-grid">{experts.map(person => <Person key={person.id} person={person} />)}</div></section>}
      {error && <Notice error>{error}</Notice>}
    </div>
    <div className="panel-footer"><a className="button secondary" href="#/" onClick={() => edit({ topic: state.topic, expert_count: state.expert_count })}>返回修改</a>
      {state.status === 'awaiting_confirmation' && <>
        <button className="button secondary" onClick={() => void submit('regenerate')} disabled={!!pending}>{pending === 'regenerate' ? '正在提交…' : '重新生成'}</button>
        <button className="button primary" onClick={() => void submit('confirm')} disabled={!!pending}>{pending === 'confirm' ? '正在进入演播厅…' : '确认并开始'} <span aria-hidden="true">→</span></button>
      </>}
      {state.failure?.stage === 'roster' && <button className="button primary" onClick={() => void submit('regenerate')} disabled={!!pending}>{pending ? '正在提交…' : '重试生成'}</button>}
    </div>
  </section>
}

type Pane = 'transcript' | 'experts' | 'findings'
const paneNames: Record<Pane, string> = { transcript: '发言实录', experts: '专家席', findings: '共识与分歧' }

function Studio({ state }: { state: Snapshot }) {
  const [pane, setPane] = useState<Pane>('transcript')
  const [side, setSide] = useState<'experts' | 'findings'>('experts')
  const transcript = useRef<HTMLDivElement>(null)
  const atEnd = useRef(true)
  const people = new Map(state.participants.map(person => [person.id, person]))
  const experts = state.participants.filter(person => person.role === 'expert')
  const host = state.participants.find(person => person.role === 'host')
  useLayoutEffect(() => {
    const region = transcript.current
    if (region && atEnd.current) region.scrollTop = region.scrollHeight
  }, [state.utterances.length, state.summary, pane])

  return <div className={`studio-body pane-${pane} side-${side}`}>
    <nav className="pane-switch mobile-switch" aria-label="演播厅面板">{(Object.keys(paneNames) as Pane[]).map(item => <button key={item} type="button" aria-pressed={item === pane} onClick={() => setPane(item)}>{paneNames[item]}</button>)}</nav>
    <nav className="pane-switch desktop-switch" aria-label="演播厅侧栏">{(['experts', 'findings'] as const).map(item => <button key={item} type="button" aria-pressed={item === side} onClick={() => setSide(item)}>{paneNames[item]}</button>)}</nav>
    <section className="panel experts">
      <div className="panel-heading"><span className="section-icon" aria-hidden="true">◎</span><h2>专家席</h2><span className="total">{experts.length} 位</span></div>
      <div className="panel-body expert-list">{host && <div className="host">主持人 <strong>{host.name}</strong><small>{host.title}</small></div>}{experts.map(person => <Person key={person.id} person={person} live />)}</div>
    </section>
    <section className="panel transcript">
      <div className="panel-heading"><span className="section-icon" aria-hidden="true">≡</span><h2>发言实录</h2><span className="total">{state.utterances.length} 条</span></div>
      <div className="panel-body transcript-feed" ref={transcript} onScroll={event => { const box = event.currentTarget; atEnd.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48 }}>
        {state.utterances.length === 0 && <div className="empty" role="status">等待主持人开场…</div>}
        {state.utterances.map(item => {
          const person = people.get(item.participant_id)
          return <article className="utterance" key={item.id} style={{ '--person-color': person?.color ?? '#62786d' } as CSSProperties}>
            <div className="utterance-meta"><strong>{person?.name ?? '嘉宾'}</strong><small>{person?.title}</small><time dateTime={item.created_at}>{clock(item.created_at)}</time></div><p>{item.text}</p>
          </article>
        })}
        {state.status === 'finished' && state.summary && <article className="summary"><h3>主持人总结</h3>{state.summary.split(/\n+/).filter(Boolean).map((part, index) => <p key={index}>{part}</p>)}</article>}
      </div>
    </section>
    <section className="panel findings">
      <div className="panel-heading"><span className="section-icon" aria-hidden="true">◇</span><h2>共识与分歧</h2></div>
      <div className="panel-body finding-list" aria-live="polite">{(['consensus', 'disagreement'] as const).map(kind => {
        const items = state.findings.filter(item => item.kind === kind)
        const label = kind === 'consensus' ? '共识' : '分歧'
        return <section className={`finding-group ${kind}`} key={kind}><h3><i />{label}</h3>
          {items.length === 0 && <p className="muted">暂无明确{label}</p>}
          {items.map(item => <article key={item.id}><h4>{item.statement}</h4><ul>{item.evidence.map(evidence => {
            const speaker = state.utterances.find(utterance => utterance.id === evidence.utterance_id)?.participant_id
            const name = speaker ? people.get(speaker)?.name : undefined
            return <li key={evidence.utterance_id}>{name && <strong>{name} · </strong>}{evidence.position_text}</li>
          })}</ul></article>)}
        </section>
      })}</div>
    </section>
  </div>
}

function Discussion({ id, edit }: { id: string; edit: (draft: Draft) => void }) {
  const { state, error, connection, refresh } = useDiscussion(id)
  if (!state) return <main className="room"><section className="panel initial">{error ? <><Notice error>{error}</Notice><button className="button secondary" onClick={() => refresh(true)}>重试加载</button></> : <Loading>正在加载讨论…</Loading>}</section></main>
  const live = state.started_at !== null
  return <main className={live ? 'room room-live' : 'room'} aria-label={live ? '演播厅' : '阵容确认页'}>
    <div className="room-title"><div><div className="room-meta"><span>{live ? '演播厅' : '发起讨论'}</span><Tag status={state.status} /></div><h1 title={state.topic}>{state.topic}</h1></div></div>
    {connection !== 'open' && <div className="connection"><Notice>{error || (connection === 'retrying' ? '正在同步讨论…' : '连接中断。')}</Notice>{connection === 'offline' && <button className="button secondary" onClick={() => refresh()}>重新连接</button>}</div>}
    {state.notice && <Notice>模型响应超时，正在重试。</Notice>}
    {state.failure?.stage === 'discussion' && <Notice error>{failureMessage(state.failure)}</Notice>}
    {live ? <Studio state={state} /> : <Roster state={state} refresh={() => refresh(true)} edit={edit} />}
  </main>
}

function App() {
  const [id, setId] = useState(currentRoom)
  const [theme, setTheme] = useState<Theme>('paper')
  const [draft, setDraft] = useState<Draft>({ topic: '', expert_count: 4 })
  useEffect(() => {
    const change = () => setId(currentRoom())
    window.addEventListener('hashchange', change)
    return () => window.removeEventListener('hashchange', change)
  }, [])
  return <div className="app-shell" data-theme={theme}>
    <Header room={!!id} theme={theme} onThemeChange={setTheme} />
    {id ? <Discussion key={id} id={id} edit={setDraft} /> : <Home draft={draft} change={setDraft} />}
  </div>
}

createRoot(document.getElementById('root')!).render(<App />)
