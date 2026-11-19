import assert from 'node:assert/strict'
import { test } from 'node:test'
import { applyEvent } from '../src/contract.ts'

const timestamp = '2026-01-01T00:00:00Z'

function snapshot() {
  return {
    id: 'room', topic: 'Replay test', expert_count: 1, status: 'running',
    roster_version: 1, created_at: timestamp, started_at: timestamp, ended_at: null,
    failure: null, notice: null, summary: null, event_cursor: 10,
    participants: [{
      id: 'expert', role: 'expert', position: 1, name: 'Expert', title: 'Researcher',
      stance: 'Support', color: '#278776', public_status: 'idle', public_focus: null,
    }],
    utterances: [], findings: [],
  }
}

function event(id, type, payload) {
  return { id, type, data: { discussion_id: 'room', occurred_at: timestamp, ...payload } }
}

test('replay skips duplicate frames and displays each utterance once', () => {
  const initial = snapshot()
  const utterance = {
    id: 'speech-1', participant_id: 'expert', ordinal: 1, kind: 'expert',
    text: 'Start with a pilot.', created_at: timestamp,
  }
  const frame = event(11, 'utterance.created', { utterance })
  const first = applyEvent(initial, frame)
  assert.strictEqual(applyEvent(first, frame), first)
  assert.throws(() => applyEvent(initial, { ...frame, id: 12 }))

  const repeatedSpeech = applyEvent(first, { ...frame, id: 12 })
  assert.equal(repeatedSpeech.event_cursor, 12)
  assert.deepEqual(repeatedSpeech.utterances, [utterance])
})

test('participant status frames preserve preparing, speaking and idle in order', () => {
  let state = snapshot()
  for (const [offset, status] of ['preparing', 'speaking', 'idle'].entries()) {
    const focus = status === 'idle' ? null : 'Pilot design'
    const frame = event(11 + offset, 'participant.status', {
      participant_id: 'expert', public_status: status, public_focus: focus,
    })
    state = applyEvent(state, frame)
    assert.equal(state.event_cursor, frame.id)
    assert.equal(state.participants[0].public_status, status)
    assert.equal(state.participants[0].public_focus, focus)
  }
})
