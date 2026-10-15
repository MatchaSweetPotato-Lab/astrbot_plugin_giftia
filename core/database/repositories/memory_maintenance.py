"""Atomic memory writes, optimistic concurrency, and maintenance snapshots."""

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import aiosqlite

from .base import BaseRepository

_MEMORY_METADATA_SQL = "CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END"
ATOMIC_MEMORY_SQL = f"json_extract({_MEMORY_METADATA_SQL}, '$.schema_version')=2"
# Prefer explicit participant lists; legacy records fall back to their account.
MEMORY_PARTICIPANT_SQL = (
    "EXISTS (SELECT 1 FROM json_each(CASE "
    f"WHEN json_type({_MEMORY_METADATA_SQL}, '$.associated_user_ids')='array' "
    f"AND json_array_length({_MEMORY_METADATA_SQL}, '$.associated_user_ids')>0 "
    f"THEN json_extract({_MEMORY_METADATA_SQL}, '$.associated_user_ids') "
    f"ELSE json_array(json_extract({_MEMORY_METADATA_SQL}, '$.user_id')) END) "
    "AS participant WHERE participant.type IN ('text', 'integer') "
    "AND trim(CAST(participant.value AS TEXT)) IN (SELECT value FROM json_each(?)))"
)


class MemoryMaintenanceRepository(BaseRepository):
    def __init__(self, conn):
        super().__init__(conn)
        self.write_lock = asyncio.Lock()

    async def snapshot(
        self,
        bot_name: str,
        session_id: str,
        limit: int = 100,
        pending: bool = False,
        reviewed_only: bool = False,
    ) -> list[dict]:
        """Read a bounded, scoped snapshot for a model maintenance call.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning group or private session.
            limit: Maximum number of atoms.
            pending: Prefer oldest records not reviewed at their current revision.
            reviewed_only: Read previously reviewed context without consuming backlog.

        Returns:
            Authoritative SQLite rows with revisions and provenance.
        """
        condition = "AND revision > reviewed_revision" if pending else ""
        if reviewed_only:
            condition += " AND reviewed_revision >= revision"
        order = (
            "id ASC"
            if pending
            else "CASE kind WHEN 'agreement' THEN 0 WHEN 'preference' THEN 1 ELSE 2 END, id DESC"
        )
        async with self.conn.execute(
            f"SELECT * FROM memories WHERE bot_name=? AND group_or_user_id=? "
            f"AND status='active' {condition} ORDER BY {order} LIMIT ?",
            (bot_name, session_id, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def related_snapshot(
        self,
        bot_name: str,
        session_id: str,
        query: str,
        memory_ids: list[str],
        limit: int = 5,
        exclude_ids: set[str] | None = None,
        history_end: int | None = None,
        text_fallback: bool = True,
        associated_user_ids: set[str] | None = None,
    ) -> list[dict]:
        """Hydrate semantic hits and supplement them with full-history text matches.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            query: One source message or memory being reviewed.
            memory_ids: Semantic candidates in relevance order.
            limit: Maximum number of related records.
            exclude_ids: Seed records already included in the model input.
            history_end: Last historical row before the extraction batch.
            text_fallback: Search text when semantic retrieval is disabled.
            associated_user_ids: Require an exact overlap with these member accounts.

        Returns:
            Current active historical rows, including legacy records.
        """
        excluded = sorted(exclude_ids or ())
        scope = (
            "bot_name=? AND group_or_user_id=? AND status='active' "
            "AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))"
        )
        scope_params = [bot_name, session_id]
        if associated_user_ids is not None:
            scope += f" AND {MEMORY_PARTICIPANT_SQL}"
            scope_params.append(json.dumps(sorted(associated_user_ids)))
        if not text_fallback:
            scope += " AND indexed_revision=revision"
        if history_end is not None:
            scope += " AND id<=?"
            scope_params.append(history_end)
        if excluded:
            scope += f" AND memory_id NOT IN ({','.join('?' for _ in excluded)})"
            scope_params.extend(excluded)
        candidates = list(dict.fromkeys(memory_ids))
        related = []
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            async with self.conn.execute(
                f"SELECT * FROM memories WHERE {scope} AND memory_id IN ({placeholders})",
                (*scope_params, *candidates),
            ) as cursor:
                current = {
                    row["memory_id"]: dict(row) for row in await cursor.fetchall()
                }
            related = [current[key] for key in candidates if key in current][:limit]
        if len(related) >= limit or not text_fallback:
            return related

        # Overlapping CJK pairs match Chinese text without a tokenizer dependency.
        # Sample across long queries rather than discarding their final topics.
        terms = []
        for word in re.findall(r"[^\W_]+", query.casefold()):
            if re.search(r"[\u3400-\u9fff]", word):
                terms.extend(word[index : index + 2] for index in range(len(word) - 1))
            else:
                terms.append(word)
        terms = list(dict.fromkeys(terms))
        if not terms:
            return related
        terms = terms[:: max(1, (len(terms) + 63) // 64)][:64]
        score = " + ".join("(instr(lower(text), ?) > 0)" for _ in terms)
        async with self.conn.execute(
            f"WITH matches AS (SELECT *, ({score}) AS relevance FROM memories WHERE {scope}) "
            "SELECT * FROM matches WHERE relevance>0 "
            "ORDER BY relevance DESC, importance DESC, id ASC LIMIT ?",
            (*terms, *scope_params, limit + len(related)),
        ) as cursor:
            matched = [dict(row) for row in await cursor.fetchall()]
        seen = {row["memory_id"] for row in related}
        related.extend(row for row in matched if row["memory_id"] not in seen)
        return related[:limit]

    async def pending_consolidation(
        self, bot_name: str, session_id: str, include_suspended: bool = False
    ) -> dict | None:
        """Read the oldest durable job created by a successful extraction.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            include_suspended: Include failed batches for an explicit retry.

        Returns:
            Queue key, original value and payload, or None when no job is pending.
        """
        condition = (
            ""
            if include_suspended
            else "AND json_extract(value, '$.status') IS NOT 'suspended' "
        )
        async with self.conn.execute(
            "SELECT key, value FROM kv_store WHERE key LIKE 'atomic_memory:consolidation:%' "
            "AND json_extract(value, '$.bot_name')=? AND json_extract(value, '$.session_id')=? "
            f"{condition}ORDER BY created_at, key LIMIT 1",
            (bot_name, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        return (
            {
                "key": row["key"],
                "value": row["value"],
                "payload": json.loads(row["value"]),
            }
            if row
            else None
        )

    async def suspend_consolidation(self, job: dict, failure: dict) -> bool:
        """Retain a rejected batch and diagnostics without blocking later jobs.

        Args:
            job: Exact queued job snapshot used for the rejected inference.
            failure: System prompt, attempted inputs, responses and errors.

        Returns:
            Whether the unchanged queued job was suspended.
        """
        now = datetime.now(timezone.utc).isoformat()
        value = json.dumps(
            {
                **job["payload"],
                "status": "suspended",
                "suspended_at": now,
                "failure": failure,
            },
            ensure_ascii=False,
        )
        async with self.write_lock:
            # A stale inference must not overwrite concurrent queue progress.
            cursor = await self.conn.execute(
                "UPDATE kv_store SET value=?, updated_at=? WHERE key=? AND value=?",
                (value, now, job["key"], job["value"]),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def apply_changes(
        self,
        bot_name: str,
        session_id: str,
        changes: list[dict],
        expected: dict[str, int],
        batch_key: str = "",
        reviewed: bool = False,
        source_end: int | None = None,
        reviewed_ids: set[str] | None = None,
        observer_id: str = "",
        consolidation_job: dict | None = None,
        processed_ids: set[str] | None = None,
    ) -> list[str]:
        """Commit a validated batch and its checkpoint in one transaction.

        Args:
            bot_name: Authoritative bot partition.
            session_id: Authoritative conversation partition.
            changes: Validated atoms and their provenance.
            expected: Revisions of the exact model input snapshot.
            batch_key: Idempotency key for an incremental source interval.
            reviewed: Mark input revisions and derived outputs as consolidated.
            source_end: Last source message handled by an incremental batch.
            reviewed_ids: Seed records to checkpoint; None checkpoints all inputs.
            observer_id: Observer persisted with a newly extracted batch.
            consolidation_job: Exact queued job whose progress is being committed.
            processed_ids: New records checked by this consolidation attempt.

        Returns:
            IDs of inserted, updated, or superseded atoms.

        Raises:
            ValueError: A memory changed or disappeared during model inference.
        """
        async with self.write_lock:
            async with self.conn.execute("PRAGMA database_list") as cursor:
                database_file = (await cursor.fetchone())[2]
            # Other repositories commit on the shared connection. A separate file
            # connection prevents those commits from splitting this transaction.
            writer = (
                await aiosqlite.connect(database_file) if database_file else self.conn
            )
            writer.row_factory = aiosqlite.Row
            try:
                await writer.execute("BEGIN IMMEDIATE")
                if batch_key:
                    async with writer.execute(
                        "SELECT 1 FROM kv_store WHERE key=?", (batch_key,)
                    ) as cursor:
                        if await cursor.fetchone():
                            await writer.rollback()
                            return []
                if consolidation_job:
                    async with writer.execute(
                        "SELECT value FROM kv_store WHERE key=?",
                        (consolidation_job["key"],),
                    ) as cursor:
                        queued = await cursor.fetchone()
                    payload = consolidation_job["payload"]
                    if (
                        queued is None
                        or queued["value"] != consolidation_job["value"]
                        or (payload["bot_name"], payload["session_id"])
                        != (bot_name, session_id)
                    ):
                        raise ValueError("Consolidation job changed during inference")
                for memory_id, revision in expected.items():
                    async with writer.execute(
                        "SELECT revision FROM memories WHERE memory_id=? AND bot_name=? AND group_or_user_id=?",
                        (memory_id, bot_name, session_id),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is None or row["revision"] != revision:
                        raise ValueError("Memory snapshot changed during maintenance")
                if source_end is not None:
                    async with writer.execute(
                        "SELECT 1 FROM chat_history WHERE id=? AND bot_name=? AND group_or_user_id=?",
                        (source_end, bot_name, session_id),
                    ) as cursor:
                        if not await cursor.fetchone():
                            raise ValueError(
                                "Source interval was removed during inference"
                            )
                now = datetime.now(timezone.utc).isoformat()
                affected = []
                inserted = []
                async with writer.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM memories WHERE bot_name=? AND group_or_user_id=?",
                    (bot_name, session_id),
                ) as cursor:
                    history_end = (await cursor.fetchone())[0]
                for item in changes:
                    if batch_key and (
                        item["memory_id"]
                        or item["source_memory_ids"]
                        or item["supersedes"]
                    ):
                        raise ValueError("Extraction may only insert new memories")
                    memory_id = item["memory_id"] or str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            json.dumps([bot_name, session_id, item["fingerprint"]]),
                        )
                    )
                    async with writer.execute(
                        "SELECT metadata, revision FROM memories WHERE memory_id=? AND bot_name=? AND group_or_user_id=?",
                        (memory_id, bot_name, session_id),
                    ) as cursor:
                        old = await cursor.fetchone()
                    if batch_key and old:
                        continue
                    try:
                        previous = json.loads(old["metadata"] or "{}") if old else {}
                    except (ValueError, TypeError):
                        previous = {}
                    if not isinstance(previous, dict):
                        previous = {}
                    # Historical provenance survives even when its source atoms
                    # were not recalled into this particular model input.
                    inherited = previous.get("source_memory_ids", [])
                    references = [
                        *(
                            ref
                            for ref in item["source_memory_ids"]
                            if ref not in inherited
                        ),
                        *item["supersedes"],
                    ]
                    if item["memory_id"]:
                        references.append(item["memory_id"])
                    if any(ref not in expected for ref in references):
                        raise ValueError(
                            "Memory writes must reference the scoped snapshot"
                        )
                    for source_id in item.get("new_source_ids", []):
                        async with writer.execute(
                            "SELECT 1 FROM chat_history WHERE id=? AND bot_name=? AND group_or_user_id=? "
                            "AND COALESCE(is_recalled, 0)=0 AND COALESCE(role, 'message')!='operation_log'",
                            (source_id, bot_name, session_id),
                        ) as cursor:
                            if not await cursor.fetchone():
                                raise ValueError(
                                    "A source message disappeared or was recalled"
                                )
                    metadata = {
                        key: value
                        for key, value in item.items()
                        if key
                        not in {
                            "memory_id",
                            "text",
                            "importance",
                            "fingerprint",
                            "supersedes",
                            "kind",
                            "status",
                            "expires_at",
                            "new_source_ids",
                        }
                    }
                    metadata.update(
                        schema_version=2, user_id=item["associated_user_ids"][0]
                    )
                    metadata.update(
                        kind=item["kind"],
                        status=item["status"],
                        expires_at=item["expires_at"],
                    )
                    metadata = {**previous, **metadata}
                    revision = old["revision"] + 1 if old else 1
                    await writer.execute(
                        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata, importance, "
                        "kind, status, expires_at, revision, reviewed_revision, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET text=excluded.text, metadata=excluded.metadata, "
                        "importance=excluded.importance, kind=excluded.kind, status=excluded.status, expires_at=excluded.expires_at, "
                        "revision=excluded.revision, reviewed_revision=excluded.reviewed_revision, vector=NULL, updated_at=excluded.updated_at",
                        (
                            bot_name,
                            session_id,
                            memory_id,
                            item["text"],
                            json.dumps(metadata, ensure_ascii=False),
                            item["importance"],
                            item["kind"],
                            item["status"],
                            item["expires_at"] or None,
                            revision,
                            revision if reviewed else 0,
                            now,
                            now,
                        ),
                    )
                    affected.append(memory_id)
                    if old is None:
                        inserted.append(memory_id)
                    for old_id in item["supersedes"]:
                        await writer.execute(
                            "UPDATE memories SET status='superseded', revision=revision+1, updated_at=? "
                            "WHERE memory_id=? AND bot_name=? AND group_or_user_id=? AND status='active'",
                            (now, old_id, bot_name, session_id),
                        )
                        affected.append(old_id)
                if reviewed:
                    for memory_id, revision in expected.items():
                        if reviewed_ids is not None and memory_id not in reviewed_ids:
                            continue
                        await writer.execute(
                            "UPDATE memories SET reviewed_revision=revision WHERE memory_id=? "
                            "AND bot_name=? AND group_or_user_id=? AND revision=?",
                            (memory_id, bot_name, session_id, revision),
                        )
                if batch_key:
                    await writer.execute(
                        "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, 1, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=1, updated_at=excluded.updated_at",
                        (batch_key, now, now),
                    )
                    if inserted:
                        await writer.execute(
                            "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)",
                            (
                                f"atomic_memory:consolidation:{batch_key}",
                                json.dumps(
                                    {
                                        "bot_name": bot_name,
                                        "session_id": session_id,
                                        "self_id": str(observer_id),
                                        "memory_ids": inserted,
                                        "pending_ids": inserted,
                                        "history_end": history_end,
                                    }
                                ),
                                now,
                                now,
                            ),
                        )
                if consolidation_job:
                    payload = consolidation_job["payload"]
                    remaining = [
                        key
                        for key in payload["pending_ids"]
                        if key not in (processed_ids or ())
                    ]
                    if remaining:
                        remaining_payload = {**payload, "pending_ids": remaining}
                        remaining_payload.pop("status", None)
                        remaining_payload.pop("suspended_at", None)
                        await writer.execute(
                            "UPDATE kv_store SET value=?, updated_at=? WHERE key=?",
                            (
                                json.dumps(remaining_payload),
                                now,
                                consolidation_job["key"],
                            ),
                        )
                    else:
                        await writer.execute(
                            "DELETE FROM kv_store WHERE key=?",
                            (consolidation_job["key"],),
                        )
                if source_end is not None:
                    await writer.execute(
                        "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(kv_store.value AS INTEGER), excluded.value), "
                        "updated_at=excluded.updated_at",
                        (
                            f"passive_memory:last_summarized_id:{bot_name}:{session_id}",
                            source_end,
                            now,
                            now,
                        ),
                    )
                await writer.commit()
                return affected
            except BaseException:
                await writer.rollback()
                raise
            finally:
                if database_file:
                    await writer.close()

    async def expire_agreements(self, bot_name: str, session_id: str) -> int:
        """Expire elapsed agreements without claiming they were fulfilled.

        Args:
            bot_name: Owning bot.
            session_id: Owning conversation.

        Returns:
            Number of agreements whose validity ended.
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "UPDATE memories SET status='expired', revision=revision+1, updated_at=? "
            "WHERE bot_name=? AND group_or_user_id=? AND kind='agreement' AND status='active' "
            "AND expires_at IS NOT NULL AND julianday(expires_at)<=julianday(?) "
            f"AND {ATOMIC_MEMORY_SQL}",
            (now, bot_name, session_id, now),
        )
        await self.conn.commit()
        return cursor.rowcount
