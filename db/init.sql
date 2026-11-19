PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE discussions (
    id TEXT PRIMARY KEY NOT NULL,
    topic TEXT NOT NULL CHECK (length(trim(topic)) BETWEEN 1 AND 200),
    expert_count INTEGER NOT NULL DEFAULT 4 CHECK (expert_count BETWEEN 2 AND 6),
    status TEXT NOT NULL CHECK (status IN ('generating', 'awaiting_confirmation', 'running', 'finished', 'failed')),
    roster_version INTEGER NOT NULL DEFAULT 1 CHECK (roster_version >= 1),
    event_cursor INTEGER NOT NULL DEFAULT 0 CHECK (event_cursor BETWEEN 0 AND 9007199254740991),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    summary TEXT CHECK (summary IS NULL OR length(trim(summary)) > 0),
    failure_stage TEXT CHECK (failure_stage IN ('roster', 'discussion')),
    failure_code TEXT,
    CHECK (
        (status IN ('generating', 'awaiting_confirmation', 'running', 'finished')
            AND failure_stage IS NULL AND failure_code IS NULL)
        OR (status = 'failed' AND failure_stage IS NOT NULL AND failure_code IS NOT NULL)
    ),
    CHECK ((started_at IS NOT NULL) =
        (status IN ('running', 'finished') OR coalesce(failure_stage = 'discussion', 0))),
    CHECK ((ended_at IS NOT NULL) = (status IN ('finished', 'failed'))),
    CHECK ((summary IS NOT NULL) = (status = 'finished'))
) STRICT;

CREATE INDEX discussions_running ON discussions (started_at DESC, id DESC)
    WHERE status = 'running';

CREATE TABLE participants (
    discussion_id TEXT NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('host', 'expert')),
    position INTEGER NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    stance TEXT NOT NULL CHECK (length(trim(stance)) > 0),
    color TEXT NOT NULL COLLATE NOCASE CHECK (
        length(color) = 7 AND substr(color, 1, 1) = '#' AND substr(color, 2) NOT GLOB '*[^0-9a-fA-F]*'
    ),
    public_status TEXT CHECK (public_status IN ('idle', 'preparing', 'speaking')),
    public_focus TEXT,
    PRIMARY KEY (discussion_id, id),
    UNIQUE (discussion_id, position),
    UNIQUE (discussion_id, color),
    CHECK (
        (role = 'host' AND position = 0 AND public_status IS NULL AND public_focus IS NULL)
        OR (role = 'expert' AND position BETWEEN 1 AND 6 AND public_status IS NOT NULL)
    )
) STRICT;

CREATE TABLE utterances (
    discussion_id TEXT NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('opening', 'follow_up', 'transition', 'expert', 'closing')),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (discussion_id, id),
    UNIQUE (discussion_id, ordinal),
    FOREIGN KEY (discussion_id, participant_id) REFERENCES participants (discussion_id, id)
) STRICT;

CREATE INDEX utterances_by_speaker ON utterances (discussion_id, participant_id);
CREATE UNIQUE INDEX utterances_one_opening ON utterances (discussion_id) WHERE kind = 'opening';
CREATE UNIQUE INDEX utterances_one_closing ON utterances (discussion_id) WHERE kind = 'closing';

CREATE TABLE findings (
    discussion_id TEXT NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('consensus', 'disagreement')),
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0),
    PRIMARY KEY (discussion_id, id)
) STRICT;

CREATE TABLE finding_evidence (
    discussion_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    utterance_id TEXT NOT NULL,
    position_text TEXT NOT NULL CHECK (length(trim(position_text)) > 0),
    PRIMARY KEY (discussion_id, finding_id, utterance_id),
    FOREIGN KEY (discussion_id, finding_id) REFERENCES findings (discussion_id, id) ON DELETE CASCADE,
    FOREIGN KEY (discussion_id, utterance_id) REFERENCES utterances (discussion_id, id)
) STRICT;

CREATE INDEX finding_evidence_by_utterance ON finding_evidence (discussion_id, utterance_id);

CREATE TABLE live_events (
    discussion_id TEXT NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 9007199254740991),
    type TEXT NOT NULL CHECK (type IN (
        'discussion.status', 'participant.status', 'utterance.created', 'findings.updated',
        'discussion.notice', 'summary.created', 'discussion.finished'
    )),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    created_at TEXT NOT NULL,
    PRIMARY KEY (discussion_id, seq)
) STRICT;

COMMIT;
