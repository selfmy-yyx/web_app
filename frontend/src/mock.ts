import { applyEvent, isTerminal, type Accepted, type Card, type DiscussionApi, type EventType, type Finding, type LiveEvent, type Participant, type Payloads, type Snapshot, type Utterance } from './contract'

type Frame = { at: number; event: LiveEvent }
type Room = { initial: Snapshot; frames: Frame[] }
const storageKey = 'panel-studio-demo-v1'
const flags = new URLSearchParams(location.search)
const pause = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))
const now = () => new Date().toISOString()
const uuid = () => crypto.randomUUID()

const profiles = [
  ['林知遥', '公共议题主持人', '保持中立，梳理不同观点', '#B47B2F'],
  ['陈宁', '组织行为研究员', '从可观测的结果出发，支持先试点', '#248779'],
  ['周凯', '运营策略顾问', '主张小规模验证并保留退出路径', '#6278BE'],
  ['许岚', '风险管理顾问', '关注低估的实施成本与风险', '#BD6755'],
  ['沈悦', '产品体验研究员', '以实际使用者的反馈检验方案', '#8565A3'],
  ['贺川', '数据评估分析师', '要求明确基线与评价标准', '#3D7998'],
  ['方静', '公共政策研究员', '考虑实施责任与长期影响', '#9B7751'],
]

function roster(count: number): Participant[] {
  return profiles.slice(0, count + 1).map(([name, title, stance, color], position) => ({
    id: uuid(), role: position === 0 ? 'host' : 'expert', position, name, title, stance, color,
    public_status: position === 0 ? null : 'idle', public_focus: null,
  }))
}

function newRoom(topic: string, expert_count: number): Room {
  return { initial: {
    id: uuid(), topic, expert_count, created_at: now(), status: 'generating', roster_version: 1,
    participants: [], started_at: null, ended_at: null, failure: null, notice: null,
    utterances: [], findings: [], summary: null, event_cursor: 0,
  }, frames: [] }
}

function add<K extends EventType>(room: Room, at: number, type: K, data: Payloads[K]) {
  room.frames.push({ at, event: {
    id: room.frames.length + 1, type, data: { ...data, discussion_id: room.initial.id, occurred_at: new Date(at).toISOString() },
  } as LiveEvent })
}

function snapshot(room: Room, at = Date.now()) {
  return room.frames.filter(frame => frame.at <= at).reduce((state, frame) => applyEvent(state, frame.event), room.initial)
}

function scheduleRoster(room: Room, at: number, version: number, fail: boolean, count: number) {
  add(room, at, 'discussion.status', {
    status: fail ? 'failed' : 'awaiting_confirmation', roster_version: version,
    participants: fail ? [] : roster(count), started_at: null,
    ended_at: fail ? new Date(at).toISOString() : null,
    failure: fail ? { stage: 'roster', code: 'ROSTER_GENERATION_FAILED' } : null,
  })
}

function scheduleDiscussion(room: Room, state: Snapshot, start: number, pace = 1) {
  const started_at = new Date(start).toISOString()
  const [host, a, b, c = b] = state.participants
  add(room, start, 'discussion.status', {
    status: 'running', roster_version: state.roster_version,
    participants: state.participants, started_at, ended_at: null, failure: null,
  })
  let ordinal = 0
  const at = (offset: number) => start + offset * pace
  const say = (offset: number, speaker: Participant, kind: Utterance['kind'], text: string) => {
    const utterance = { id: uuid(), participant_id: speaker.id, ordinal: ++ordinal, kind, text, created_at: new Date(at(offset)).toISOString() }
    add(room, at(offset), 'utterance.created', { utterance })
    return utterance.id
  }
  const speak = (offset: number, person: Participant, focus: string, text: string) => {
    add(room, at(offset), 'participant.status', { participant_id: person.id, public_status: 'preparing', public_focus: focus })
    add(room, at(offset + 650), 'participant.status', { participant_id: person.id, public_status: 'speaking', public_focus: focus })
    const id = say(offset + 1250, person, 'expert', text)
    add(room, at(offset + 1350), 'participant.status', { participant_id: person.id, public_status: 'idle', public_focus: focus })
    return id
  }
  say(350, host, 'opening', '欢迎各位。我们先从可以验证的做法谈起。')
  const a1 = speak(1100, a, '用试点收集真实反馈', '我建议先做一个小规模试点，记录参与者的实际体验。')
  speak(2900, a, '给试点保留调整空间', '试点还需要预先约定复盘时间，这样发现问题后能及时调整。')
  const b1 = speak(4700, b, '用可衡量的指标评估试点', '我赞成小规模试点，但建议先定好成功标准和退出条件。')
  const consensus: Finding = { id: uuid(), kind: 'consensus', statement: '先进行小规模试点，再依据反馈调整。', evidence: [
    { utterance_id: a1, position_text: '通过试点收集实际反馈' }, { utterance_id: b1, position_text: '先约定试点成功与退出条件' },
  ] }
  add(room, at(6200), 'findings.updated', { findings: [consensus] })
  say(7450, host, 'follow_up', '在试点之前，有哪些条件需要先统一？')
  const b2 = speak(8350, b, '统一评价口径', '我倾向于由项目组先设一套统一标准，这样试点结果才有可比性。')
  const c1 = speak(10250, c === b ? a : c, '为不同场景保留弹性', '我更倾向于让参与者自行设定具体指标，因为场景和成本可能差异很大。')
  const disagreement: Finding = { id: uuid(), kind: 'disagreement', statement: '试点评价指标应统一，还是按场景设定？', evidence: [
    { utterance_id: b2, position_text: '统一标准以保证结果可比' }, { utterance_id: c1, position_text: '按不同场景设定指标' },
  ] }
  add(room, at(11750), 'findings.updated', { findings: [consensus, disagreement] })
  say(14300, host, 'closing', '我们已经形成了先试点的共识，接下来需要解决评价指标的制定方式。')
  add(room, at(14300), 'summary.created', {
    summary: '各方同意先开展小规模试点，预先设定复盘时间和退出条件，并根据真实反馈调整方案。\n\n仍有分歧的是评价指标应统一制定，还是由参与者按具体场景设定。',
  })
  add(room, at(14300), 'discussion.finished', { status: 'finished', ended_at: new Date(at(14300)).toISOString() })
}

function seed() {
  if (flags.has('empty')) return {}
  const room = newRoom('混合办公能否成为团队的长期默认？', 4)
  const start = Date.now() - 62_000
  room.initial.created_at = new Date(start - 1000).toISOString()
  scheduleRoster(room, start - 700, 1, false, 4)
  scheduleDiscussion(room, snapshot(room, start), start, 15)
  return { [room.initial.id]: room }
}

const key = flags.has('empty') ? storageKey + '-empty' : storageKey
function read(): Record<string, Room> {
  const cached = localStorage.getItem(key)
  if (cached) return JSON.parse(cached)
  const rooms = seed()
  write(rooms)
  return rooms
}

function write(rooms: Record<string, Room>) {
  localStorage.setItem(key, JSON.stringify(rooms))
}

function get(rooms: Record<string, Room>, id: string) {
  const room = rooms[id]
  if (!room) throw new Error('这场讨论不存在。')
  return room
}

export const mockApi: DiscussionApi = {
  async list() {
    await pause(350)
    const items = Object.values(read()).map(room => snapshot(room)).flatMap<Card>(state =>
      state.status === 'running' && state.started_at
        ? [{ id: state.id, status: 'running', topic: state.topic, expert_count: state.expert_count, started_at: state.started_at }]
        : [])
    return { items: items.sort((a, b) => b.started_at.localeCompare(a.started_at) || b.id.localeCompare(a.id)) }
  },
  async create({ topic, expert_count }) {
    await pause(280)
    const rooms = read()
    const room = newRoom(topic, expert_count)
    scheduleRoster(room, Date.now() + 1450, 1, flags.has('fail-roster'), expert_count)
    rooms[room.initial.id] = room
    write(rooms)
    return { id: room.initial.id, status: 'generating', roster_version: 1 }
  },
  async regenerate(id, version): Promise<Accepted> {
    await pause(240)
    const rooms = read()
    const room = get(rooms, id)
    const state = snapshot(room)
    if (state.roster_version !== version) throw new Error('阵容已更新，请刷新后重试。')
    if (state.status !== 'awaiting_confirmation' && state.failure?.stage !== 'roster') throw new Error('当前无法重新生成阵容。')
    add(room, Date.now(), 'discussion.status', {
      status: 'generating', roster_version: version + 1, participants: [], started_at: null, ended_at: null, failure: null,
    })
    scheduleRoster(room, Date.now() + 1450, version + 1, false, state.expert_count)
    write(rooms)
    return { id, status: 'generating', roster_version: version + 1 }
  },
  async confirm(id, version) {
    await pause(720)
    const rooms = read()
    const room = get(rooms, id)
    const state = snapshot(room)
    if (state.started_at && (state.status === 'running' || state.status === 'finished' || state.status === 'failed')) {
      return { id, status: state.status, started_at: state.started_at }
    }
    if (state.roster_version !== version || state.status !== 'awaiting_confirmation') throw new Error('阵容尚未就绪，请刷新后确认。')
    const start = Date.now()
    scheduleDiscussion(room, state, start)
    write(rooms)
    return { id, status: 'running', started_at: new Date(start).toISOString() }
  },
  async detail(id) {
    await pause(230)
    return snapshot(get(read(), id))
  },
  subscribe(id, after, onEvent, onConnection) {
    let cursor = after
    const timer = setInterval(() => {
      const room = get(read(), id)
      for (const { at, event } of room.frames) {
        if (at > Date.now() || event.id <= cursor) continue
        cursor = event.id
        onEvent(event)
      }
      if (isTerminal(snapshot(room))) clearInterval(timer)
    }, 100)
    onConnection('open')
    return () => clearInterval(timer)
  },
}
