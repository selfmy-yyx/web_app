# AI Panel Studio API 契约

拟使用 HTTP JSON 提供查询和操作，接口前缀为 `/api/v1`。

| 方法与路径 | 请求与结果 |
| --- | --- |
| GET /discussions | 返回进行中的讨论列表，包括场次标识、话题和专家人数。 |
| POST /discussions | 接收话题和可选的专家人数，默认 4 人。返回新场次标识和阵容生成状态。 |
| GET /discussions/{id} | 返回指定场次的公开详情，包括状态、阵容、发言、观点和可用的总结。 |
| POST /discussions/{id}/confirm | 确认该场阵容并开始讨论，返回场次状态。 |
| GET /discussions/{id}/events | 通过 SSE 订阅该场的实时更新。 |

SSE 公开场次状态、专家公开状态、正式发言、共识与分歧、总结及结束通知。访客进入或刷新时先读取场次详情，再接续实时更新。接口应对无效请求、找不到场次及阵容未就绪给出可识别的反馈。
