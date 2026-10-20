from astrbot.api import logger

from ..memory.recall_scope import (
    resolve_memory_recall_scope,
    select_automatic_recall_memories,
)


def conf_int(conf: dict, key: str, default: int) -> int:
    try:
        return int(conf.get(key, default))
    except (TypeError, ValueError):
        return default


def conf_float(conf: dict, key: str, default: float) -> float:
    try:
        return float(conf.get(key, default))
    except (TypeError, ValueError):
        return default


def resolve_memory_search_limit(
    embedding_conf: dict,
    *,
    limit_key: str | None = None,
    default: int = 5,
) -> int:
    fallback = conf_int(
        embedding_conf,
        "limit",
        conf_int(embedding_conf, "top_k", default),
    )
    if limit_key:
        return conf_int(embedding_conf, limit_key, fallback)
    return fallback


async def search_memories_with_rerank(
    plugin,
    *,
    bot_name: str,
    group_or_user_id: str,
    query: str,
    recent_messages: list,
    limit_key: str | None = None,
    default_limit: int = 5,
    log_context: str = "记忆召回",
    record_hits: bool = True,
    user_ids: list[str] | None = None,
) -> list[dict]:
    """Search memories using the shared embedding and rerank settings.

    Args:
        plugin: Plugin providing memory storage and model configuration.
        bot_name: Owning bot configuration.
        group_or_user_id: Owning conversation.
        query: Semantic search text.
        recent_messages: Messages identifying the active participants.
        limit_key: Optional configuration key overriding the candidate limit.
        default_limit: Candidate limit when no configured value is available.
        log_context: Context included in retrieval failure logs.
        record_hits: Record hits immediately; batch callers may defer until deduplication.
        user_ids: Target accounts in user scope, or an explicit narrowing filter
            in other scopes. None follows the configured recall scope.

    Returns:
        Recalled memory records, optionally filtered by the reranker.
    """
    if not plugin.embedding_conf.get("enabled", False):
        return []

    query = (query or "").strip()
    if not query:
        return []

    search_limit = resolve_memory_search_limit(
        plugin.embedding_conf,
        limit_key=limit_key,
        default=default_limit,
    )
    if search_limit <= 0:
        return []

    threshold = conf_float(plugin.embedding_conf, "threshold", 0.7)
    try:
        memories = await plugin.passive_memory_manager.search_and_filter_memories(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            recent_messages=recent_messages,
            limit=search_limit,
            threshold=threshold,
            user_ids=user_ids,
        )
        if memories and plugin.rerank_conf.get("enabled", False):
            memories = await plugin.ltm.rerank_memories(
                query=query,
                memories=memories,
                top_k=conf_int(plugin.rerank_conf, "top_k", search_limit),
                threshold=conf_float(plugin.rerank_conf, "threshold", 0.45),
            )
        if memories and record_hits:
            await plugin.data_cache.record_memory_hits(memories)
        return memories or []
    except Exception as e:
        logger.warning(f"[Giftia] {log_context}失败: {e}")
        return []


async def search_automatic_user_memories(
    plugin,
    *,
    bot_name: str,
    group_or_user_id: str,
    query: str,
    recent_messages: list,
    user_id: str,
) -> list[dict]:
    """Recall a speaker's batch with an additional user route in full scope.

    Args:
        plugin: Plugin providing memory storage and model configuration.
        bot_name: Owning bot configuration.
        group_or_user_id: Current conversation.
        query: This speaker's text and completed media captions.
        recent_messages: Messages identifying the active participants.
        user_id: Speaker account, or an empty value for an untargeted reminder.

    Returns:
        Deduplicated, relevant candidates with one user-associated slot when
        available. Hit statistics are deferred until batch-wide selection.
    """
    recall_scope = resolve_memory_recall_scope(plugin)
    if recall_scope != "all" or not user_id:
        return await search_memories_with_rerank(
            plugin,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            recent_messages=recent_messages,
            limit_key="session_recall_search_limit",
            log_context="会话记忆自动召回",
            record_hits=False,
            user_ids=[user_id] if user_id and recall_scope == "user" else None,
        )

    search_limit = resolve_memory_search_limit(
        plugin.embedding_conf, limit_key="session_recall_search_limit"
    )
    query = (query or "").strip()
    if (
        not plugin.embedding_conf.get("enabled", False)
        or search_limit <= 0
        or not query
    ):
        return []
    final_limit = search_limit
    if plugin.rerank_conf.get("enabled", False):
        final_limit = min(
            final_limit, conf_int(plugin.rerank_conf, "top_k", search_limit)
        )
    if final_limit <= 0:
        return []

    candidates = {}
    for users in ([user_id], None):
        try:
            memories = await plugin.passive_memory_manager.search_and_filter_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                query=query,
                recent_messages=recent_messages,
                limit=search_limit,
                threshold=conf_float(plugin.embedding_conf, "threshold", 0.7),
                user_ids=users,
            )
            for memory in memories:
                memory_id = str(memory.get("memory_id") or memory.get("id") or "")
                if memory_id:
                    candidates.setdefault(memory_id, memory)
        except Exception as error:
            logger.warning("[Giftia] Automatic memory recall route failed: %s", error)

    memories = list(candidates.values())
    if memories and plugin.rerank_conf.get("enabled", False):
        try:
            # Score the whole union before reserving slots or applying top-k.
            memories = await plugin.ltm.rerank_memories(
                query=query,
                memories=memories,
                top_k=len(memories),
                threshold=conf_float(plugin.rerank_conf, "threshold", 0.45),
            )
        except Exception as error:
            logger.warning("[Giftia] Automatic memory rerank failed: %s", error)
            return []
    memories.sort(
        key=lambda memory: conf_float(
            memory, "score", 1.0 - conf_float(memory, "_distance", 1.0)
        ),
        reverse=True,
    )
    return select_automatic_recall_memories(memories, [user_id], final_limit)
