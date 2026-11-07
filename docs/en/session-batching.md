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

Batch starts in the same session are at least **2 seconds** apart. Each batch holds at most **50 messages**. If the configured maximum context is smaller than 50, the batch limit decreases with it so every target fits in the reply context. Overflow remains queued. Time spent behind a running model call or the shared session lock counts toward the aggregation deadline. Expired backlog runs after the previous round finishes and the minimum interval is satisfied. The 15-second maximum bounds additional aggregation while idle; it does not bound parsing, inference or lock contention.

Reply context starts with the latest **20 messages** by default, keeps its starting point during continuous activity, and grows until it reaches the default maximum of **200 messages**. It then slides over the latest maximum-sized window. Further wakeups and replies extend the current period. Once analysis rounds and queued work are exhausted, the session is manually silenced, or it has been quiet for at least five minutes, the next wakeup starts from the minimum again. Configure `msg_history.min_msg_number` and `msg_history.msg_number` in the plugin settings. The decision model still receives 12 history messages. Background memory and affinity maintenance share the maximum message limit, processing full segments during activity and the remaining segment when the period ends. Repeated backfill does not process the same sources again.

A decision against replying completes only its own batch. Arrivals during that decision form the next batch. When a decision allows a reply, ready messages arriving before the reply starts can join it, up to the same 50-message limit. Arrivals after the reply starts remain for the next round. Separate sessions can run independently.

Select an optional review model under **正式回复 → 复审模型（可选）** in the bot editor (`llm_reply_conf.review_provider_id`); leaving it empty disables review. If new conversation messages arrive after the input is frozen, the first unsent draft can receive one consistency review before state changes or action dispatch. Only `message` content is reviewed, excluding speech, images and stickers. Approval sends the original draft and leaves arrivals queued. Rejection restores the original batch's undecided and pending states, preserves its order, merges arrivals within the 50-message limit, and reruns decision and generation. The regenerated reply is sent without another review; overflow stays queued. Each logical reply has its own single review opportunity. Actual native tool execution or later tool turns skip review. Timeouts (10 seconds) and invalid review output allow the original draft through.

Configure a separate ordered list under **机器人管理 → 编辑机器人 → 正式回复 → 无效回复回退提供商（可选）** (`llm_reply_conf.invalid_reply_fallback_provider_ids`). Leaving it empty disables this feature and preserves the existing parsing, empty-response and retry behavior without adding requests. With a configured list, empty replies, reasoning-only output and responses without recognized reply XML immediately switch to this list with the original prompt and context. Providers already attempted in the current call are skipped, and the main list's random mode does not shuffle this list. If a fallback returns empty, reasoning-only or unrecognized XML output, the reply ends immediately without retrying that provider or trying later providers. Ordinary API errors still use the normal reply retry limit and may advance to the next fallback after exhaustion. Status-only silence and valid tool or media output do not trigger fallback. Native tools that have already started are never replayed. When this feature is enabled, ending without a valid result records a failure.

To reduce proactive replies, lower the proactive trigger probability or shorten the active decision window in the bot settings. See the [detailed usage guide](../../DETAIL.md#会话批处理与消息边界) for message boundaries and failure handling.
