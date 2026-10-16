# Atomic memory management

Open Giftia WebUI from the AstrBot plugin page and select **长期记忆 (Long-term memory)**. Each record stores one fact, without memory categories or lifecycle states.

1. Select a bot and conversation, then filter by an associated member or text. Member filters match complete account IDs; member chips on a card apply the same filter.
2. Narratives describe participants, actions, interaction direction and how a fact was learned. Cards show the topic as a bold heading, with importance at the top right and manual-entry or edit markers below the heading. Factual timestamps follow the narrative; creation time and hit counts appear at the bottom. Expand the evidence section to inspect direct speech and media captions.
3. Add a memory with all associated accounts, a topic, importance and text. Multiple participants are supported. Saving selects its conversation and marks it as a manual entry without fabricating chat evidence.
4. Edit text, importance and associated accounts. Topics and original evidence remain unchanged; edits are marked as manual revisions. Refresh before editing a record changed by another request.
5. Memory cleanup inherits the selected scope, member and text filters. Preview candidates before choosing records to delete.

![Memory cards with prominent topic headings](../images/memory-cards.png)

| Previous entry | Current entry |
| --- | --- |
| Memory type/status filters and agreement state editing | Removed; record changes and agreement dates in factual narratives |
| `/整理记忆` and consolidation after extraction | Removed; no consolidation jobs are created or consumed |
| Subject/target fields | One associated-member list; narratives retain roles and direction |
| Origin filters | Removed; narratives distinguish self-report and observation, with evidence available |
| The model's memory query/delete tool | Query only; management pages and administrator commands handle deletion |
| Profile agreements, personality, interests, miscellaneous notes and relationship titles | Directly supported information is recorded as individual facts |
| Names, aliases, attitude, affinity and avatar descriptions | Continue to use profile management |
| Recent-memory injection and the recall-pool switch | One recall pool; set its injection limit to zero to disable automatic retrieval and injection |

Member cards display retained profile information directly. Filter long-term memories by account to inspect a member's facts.

![Member cards with retained information](../images/member-profile.jpg)

Historical profile fields remain in storage. Profile reports no longer read or display personality, interests, miscellaneous notes, agreements or relationship titles. Update custom report templates that reference retired variables or restore their defaults.

Extraction is controlled by the passive-maintenance setting and summary providers. Administrators can use `/强制总结` to process a bounded batch of observed, unprocessed messages. Extraction records only new facts from that batch, without retrieving, merging, superseding or rewriting historical memories, or inferring long-term habits. Changed preferences and fulfilled or cancelled agreements become new factual records. Explicit agreement dates and deadlines belong in their narratives.

Messages actually submitted to a decision or reply model, including the current batch and its historical context, count as observed. The bot's own successfully sent messages also become observed immediately after storage, without waiting for another model request. A decision against replying still counts as observation; storing other messages alone does not. Reactivation queues only the history actually read in that request, preserving gaps that were never observed. Reading a previously processed message again does not queue it again. Every automatic trigger and `/强制总结` uses this same eligibility rule.

Extraction follows the reply prompt's compact `<message>` style, retaining `source_id`, `user_id`, an available `nickname`, and the full timestamp, plus `reply_to` only when needed. It reuses the reply prompt's name resolver: prefer the name set by this bot in this conversation, otherwise preserve each message's original nickname, with the account ID as the fallback identity. Separate name tables, platform message IDs, redundant flags and empty arrays are omitted. Full dates and time zones preserve cross-day context; recalled messages, operation logs and commands are filtered before rendering. Media embedding and footnotes also reuse the reply formatter: captions whose descriptive fields total at most 100 characters and occur only once are embedded after the media placeholder as `[描述:画面: ...; 文字: ...; 类型: ...; 人物: ...; 来源: ...]`, omitting empty fields. Longer or repeated captions appear once per media ID in `<media_content>`, with genre, character, source and text attributes and the description inside `<caption>`. Only directly attached, completed captions with usable content are included. Associated-member fields retain complete IDs, and original messages and evidence are not rewritten by name resolution or media embedding.

The extraction model references each message only by `source_id`. Code snapshots direct speech and available completed media captions together with the sender, timestamp, message identifier and media identifiers. Multiple attachments still form one source. Member messages and the bot's own output can both serve as evidence, including a bot answer's `source_id` alone; member-authored evidence is not required. Pending batches containing only bot output also enter extraction. Nested quotations and forwards, recalled messages, operation logs, commands and unavailable media cannot establish direct evidence.

Silence and overflow thresholds control extraction; overflow counts only observed, pending messages outside the context window. A batch contains at most 100 complete messages within an approximate 16,000-character budget. Facts, replay protection and the exact batch's completion states commit transactionally. Model failures or validation errors retain pending sources for another trigger or `/强制总结` retry. Every five minutes, maintenance flushes conversations with observed pending messages when their last message is at least five minutes old, even if the bot is currently inactive. Conversations with no observed messages cannot start extraction, and lock contention cannot create eligibility. Maintenance also retries optional vector indexing; indexing failures do not discard saved facts.

In the decision audit, “已归档消息边界 ID” is now “已归纳消息最大 ID”, the maximum processed message ID. Each row independently shows unseen (“未见”), observed and pending (“已见待总结”), or processed (“已总结”); the maximum no longer implies that every earlier row was processed. History retention protects observed pending sources even when later backfill discovers IDs below that maximum. Migration preserves existing progress as processed, leaves other legacy messages unseen, and ignores old armed flags. Observation states survive restarts. A one-time repair uses each conversation's saved observer account to queue previously mislabelled own output, preserving unseen messages from other senders and already processed messages.

Existing memory text and evidence remain stored, without invented sources. New databases omit memory type, status, expiry-state and consolidation-progress columns. Extra columns and old consolidation jobs in existing databases are unused; initialization does not delete or rebuild historical data. Extraction follows the current JSON Schema with factual fields and message evidence only.

Decision models can request retrieval. With embeddings enabled, replies continue using the session recall pool. Old `session_recall_enabled` values are ignored; `/读取近期记忆` remains available.

Automatic retrieval runs once per reply. Pending messages are grouped by `user_id`, preserving each user's message order, and each group is searched separately. The reply prompt retains the original conversation order. Media placeholders use only completed, cached `caption` content in their original position, excluding `text`, genre, character and source fields. Media without a usable caption is skipped while ordinary text remains; an empty user group is not searched. Each user's candidates are reranked against that user's query, then merged and deduplicated by memory ID. A memory receives at most one automatic hit per batch. The per-user candidate setting controls each search, while the injection limit bounds the combined current automatic results and older pool entries, preferring current results. Setting that limit to 0 disables automatic retrieval and injection while leaving explicit decision and tool searches available. Recalls enter the pool only after the reply succeeds, with the existing TTL and capacity eviction preserved.

All retrieval paths share a renderer for factual text and available event timestamps. Observation periods in older records remain supported. Unknown event times use a separately labelled creation timestamp; edit times never substitute for event times. Prompt context omits memory types, states, internal IDs, account lists and evidence excerpts. IDs still deduplicate records, with explicit search results taking precedence over cached copies. The model retains `<search_memory>`; `<delete_memory>` is not advertised or executed.

This simplification preserves semantic retrieval, optional reranking, session-pool TTL and capacity limits. It adds neither importance/time decay nor retrieval of subsequent changes to a recalled fact.

Scheduled history cleanup and the run-now action using automatic cleanup settings protect messages beyond each bot/conversation's durable processing cursor. Missing or invalid progress, failed extraction and in-flight batches do not release pending sources. Successfully processed batches remain subject to age/count rules and minimum recent-message retention. Evidence snapshots survive source-history cleanup.
