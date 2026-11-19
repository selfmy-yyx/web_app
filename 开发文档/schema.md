# AI Panel Studio 数据模型

本版选用 SQLite 保存场次及公开讨论内容。

```mermaid
erDiagram
    DISCUSSION ||--o{ PARTICIPANT : includes
    DISCUSSION ||--o{ UTTERANCE : records
    DISCUSSION ||--o{ FINDING : tracks
    PARTICIPANT ||--o{ UTTERANCE : speaks
```

| 实体 | 主要数据 |
| --- | --- |
| 场次 | 标识、话题、专家人数、状态和总结。 |
| 参与者 | 所属场次、主持人或专家角色、姓名、头衔、立场、颜色及公开状态。 |
| 发言 | 所属场次、发言者、顺序及正文。 |
| 讨论观点 | 所属场次、共识或分歧类型、观点内容。 |

每条发言关联本场参与者。场次详情和实时更新均按场次隔离。

## v0.2 SQLite 建库

- `db/init.sql` 在空库中创建场次、参与者、发言、观点、观点证据和公开事件六张表。
- 发言和观点证据通过含场次 ID 的复合外键引用本场数据。
- `roster_version` 标识阵容版本。`event_cursor` 记录场次最新的已提交事件序号，`live_events` 按场次保存公开事件。
