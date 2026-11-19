export type Status = 'generating' | 'awaiting_confirmation' | 'running' | 'finished' | 'failed'
export type ExpertStatus = 'idle' | 'preparing' | 'speaking'
export type Failure = {
  stage: 'roster' | 'discussion'
  code: 'ROSTER_TIMEOUT' | 'ROSTER_GENERATION_FAILED' | 'MODEL_TIMEOUT' | 'MODEL_STEP_FAILED'
  operation?: 'roster' | 'intentions' | 'utterance' | 'findings' | 'summary'
  message?: string
}
export type Participant = {
  id: string; role: 'host' | 'expert'; position: number; name: string; title: string
  stance: string; color: string; public_status: ExpertStatus | null; public_focus: string | null
}
export type Utterance = {
  id: string; participant_id: string; ordinal: number; kind: 'opening' | 'follow_up' | 'transition' | 'expert' | 'closing'
  text: string; created_at: string
}
export type Finding = {
  id: string; kind: 'consensus' | 'disagreement'; statement: string
  evidence: { utterance_id: string; position_text: string }[]
}
export type Card = { id: string; topic: string; expert_count: number; status: 'running'; started_at: string }
type StatusFields = {
  status: Status; roster_version: number; participants: Participant[]
  started_at: string | null; ended_at: string | null; failure: Failure | null
}
export type Snapshot = StatusFields & {
  id: string; topic: string; expert_count: number; created_at: string
  notice: 'MODEL_RETRYING' | null; utterances: Utterance[]; findings: Finding[]
  summary: string | null; event_cursor: number
}
export type Payloads = {
  'discussion.status': StatusFields
  'participant.status': { participant_id: string; public_status: ExpertStatus; public_focus: string | null }
  'utterance.created': { utterance: Utterance }
  'findings.updated': { findings: Finding[] }
  'discussion.notice': { code: 'MODEL_RETRYING' | 'MODEL_RETRY_RESOLVED' }
  'summary.created': { summary: string }
  'discussion.finished': { status: 'finished'; ended_at: string }
}
export type EventType = keyof Payloads
export type LiveEvent<K extends EventType = EventType> = {
  [T in K]: { id: number; type: T; data: Payloads[T] & { discussion_id: string; occurred_at: string } }
}[K]
export const eventTypes = [
  'discussion.status', 'participant.status', 'utterance.created', 'findings.updated',
  'discussion.notice', 'summary.created', 'discussion.finished',
] as const satisfies readonly EventType[]

export type Draft = { topic: string; expert_count: number }
export type Accepted = { id: string; status: 'generating'; roster_version: number }
export type Confirmed = { id: string; status: 'running' | 'finished' | 'failed'; started_at: string }
export interface DiscussionApi {
  list(): Promise<{ items: Card[] }>
  create(draft: Draft): Promise<Accepted>
  regenerate(id: string, version: number): Promise<Accepted>
  confirm(id: string, version: number): Promise<Confirmed>
  detail(id: string): Promise<Snapshot>
  subscribe(id: string, after: number, onEvent: (event: LiveEvent) => number | null, onConnection: (state: 'open' | 'retrying' | 'reset') => void): () => void
}

export function isTerminal(state: Snapshot) {
  return state.status === 'finished' || state.failure?.stage === 'discussion'
}

export function applyEvent(state: Snapshot, event: LiveEvent): Snapshot {
  if (event.data.discussion_id !== state.id || event.id <= state.event_cursor) return state
  if (!Number.isSafeInteger(event.id) || event.id !== state.event_cursor + 1) throw new Error('直播序号不连续')
  const next = { ...state, event_cursor: event.id }
  switch (event.type) {
    case 'discussion.status': {
      const { status, roster_version, participants, started_at, ended_at, failure } = event.data
      return { ...next, status, roster_version, participants, started_at, ended_at, failure, notice: null }
    }
    case 'participant.status': {
      const { participant_id, public_status, public_focus } = event.data
      if (!next.participants.some(person => person.id === participant_id && person.role === 'expert')) throw new Error('专家不存在')
      return { ...next, participants: next.participants.map(person => person.id === participant_id ? { ...person, public_status, public_focus } : person) }
    }
    case 'utterance.created': {
      const { utterance } = event.data
      if (!next.participants.some(person => person.id === utterance.participant_id)) throw new Error('发言者不存在')
      if (next.utterances.some(item => item.id === utterance.id)) return next
      return { ...next, utterances: [...next.utterances, utterance].sort((a, b) => a.ordinal - b.ordinal) }
    }
    case 'findings.updated': return { ...next, findings: event.data.findings }
    case 'discussion.notice': return { ...next, notice: event.data.code === 'MODEL_RETRYING' ? event.data.code : null }
    case 'summary.created': return { ...next, summary: event.data.summary }
    case 'discussion.finished': return { ...next, status: 'finished', ended_at: event.data.ended_at, notice: null }
  }
}
