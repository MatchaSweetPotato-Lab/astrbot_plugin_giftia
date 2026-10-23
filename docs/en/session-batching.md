# Dynamic session batching

Giftia runs decisions and replies serially for each bot and full platform session. Group messages, private messages and mentions share the same automatic aggregation policy.

Look up the ready pending message count and combined body character count separately, then use the shorter wait:

| Wait | Message count | Body characters |
| --- | --- | --- |
| 15 seconds | 1 | 0–10 |
| 10 seconds | 2 | 11–30 |
| 6 seconds | 3–4 | 31–60 |
| 3 seconds | 5–7 | 61–100 |
| 1.5 seconds | 8–11 | 101–160 |
| 0.8 seconds | 12 or more | 161 or more |

Body characters come from the original message text. Whitespace, mention IDs and names, quoted content, media identifiers and generated captions do not count. Duplicate QQ Markdown mention fallbacks count only once. A mention-only or media-only message still counts as one message with zero body characters.

The deadline starts when the first parsed message is ready. Later arrivals can only bring it forward. For example, one short message initially waits 15 seconds. A second short message arriving four seconds later reduces the total wait to 10 seconds, leaving six seconds. If new content moves the deadline into the past, processing can start immediately.

Batch starts in the same session are at least **2 seconds** apart. Each batch holds at most **50 messages**, and overflow remains queued. Time spent behind a running model call or the shared session lock counts toward the aggregation deadline. Expired backlog runs after the previous round finishes and the minimum interval is satisfied. The 15-second maximum bounds additional aggregation while idle; it does not bound parsing, inference or lock contention.

A decision against replying completes only its own batch. Arrivals during that decision form the next batch. When a decision allows a reply, ready messages arriving before the reply starts can join it, up to the same 50-message limit. Arrivals after the reply starts remain for the next round. Separate sessions can run independently.

Select an optional review model under **正式回复 → 复审模型（可选）** in the bot editor (`llm_reply_conf.review_provider_id`); leaving it empty disables review. If new conversation messages arrive after the input is frozen, the first unsent draft can receive one consistency review before state changes or action dispatch. Only `message` content is reviewed, excluding speech, images and stickers. Approval sends the original draft and leaves arrivals queued. Rejection restores the original batch's undecided and pending states, preserves its order, merges arrivals within the 50-message limit, and reruns decision and generation. The regenerated reply is sent without another review; overflow stays queued. Each logical reply has its own single review opportunity. Actual native tool execution or later tool turns skip review. Timeouts (30 seconds) and invalid review output allow the original draft through.

The “会话批处理” (session batching) settings group, stored as `concurrent_config`, has been removed:

| Former setting (stored key) | Current behavior |
| --- | --- |
| Minimum decision interval (`group_throttle_time`) | Fixed at 2 seconds |
| Message debounce (`user_debounce_time`) | Automatic 0.8–15 seconds based on message and character counts |
| Maximum aggregation wait (`user_max_debounce_time`) | Up to 15 additional seconds from the first ready message; later arrivals never extend the deadline |
| Maximum messages per batch (`batch_max_messages`) | Fixed at 50; overflow stays queued |

Saved legacy values are ignored and require no migration. To reduce proactive replies, lower the proactive trigger probability or shorten the active decision window in the bot settings. See the [detailed usage guide](../../DETAIL.md#会话批处理与消息边界) for message boundaries and failure handling.
