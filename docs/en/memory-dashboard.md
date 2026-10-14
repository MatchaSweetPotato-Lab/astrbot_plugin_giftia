# Atomic memory dashboard

Open Giftia WebUI from the AstrBot plugin page and select **长期记忆 (Long-term memory)**. The default status is **有效 (Active)**. Select all statuses to inspect completed, cancelled, expired or superseded records.

1. Choose a bot and conversation, then filter by type, origin, complete account ID or content. Clicking a member badge filters by that account.
2. Habits distinguish self-reports (**本人自述**) from consolidated observations (**整理归纳**). Cards show the subject, target, topic and observation period. Expand **查看依据 (Evidence)** to read original excerpts and their source identifiers.
3. **新增记忆 (Add)** supports events, agreements, relationships, preferences/boundaries and habits. Enter the subject, optional target, topic and text. Relationships require two different accounts. Only agreements have an optional expiry, entered in the browser’s local timezone. Saved entries are shown under the manual origin filter.
4. **编辑 (Edit)** changes text, importance and associated members. Active agreements can be marked completed or cancelled. Identity and original evidence remain intact; edits are marked as manual revisions. A stale editor cannot overwrite a newer background update.
5. **清理记忆 (Clean)** inherits the current type, status and origin. Preview the candidates before selecting records to delete.

| Previous location | Current location |
| --- | --- |
| Key agreements in user profiles | Long-term memory → Agreement, with explicit status and expiry |
| Personality, interests, miscellaneous notes and relationship titles | Fixed fields retired; evidenced facts belong to event, relationship, preference or habit memories |
| Names, aliases, attitude, affinity and avatar descriptions | Remain in profile management |

Historical profile fields remain in storage and compatible report templates. Legacy memories are labelled as legacy records without fabricated evidence. Automatic extraction and consolidation remain controlled by the backend settings and administrator commands `/强制总结` and `/整理记忆`.

The screenshot uses synthetic data to compare self-reported, manually entered and inferred habits.

![Habit origins and supporting evidence](../images/atomic-memory.jpg)

Scheduled history cleanup and the run-now action using the automatic cleanup settings retain messages beyond each bot/conversation’s durable memory-processing cursor. Missing or invalid progress, failed extraction and in-flight batches do not release pending messages for deletion. Successfully handled batches remain subject to age/count rules and the minimum recent-message retention. Evidence snapshots stored inside memories survive history cleanup and remain available for habit consolidation.
