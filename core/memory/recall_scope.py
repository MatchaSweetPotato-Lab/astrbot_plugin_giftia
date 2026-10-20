"""Shared recall policy for retrieval and cached context."""

from .atomic import memory_metadata


def resolve_memory_recall_scope(plugin) -> str:
    """Read the configured scope with the existing user scope as fallback.

    Args:
        plugin: Plugin exposing the memory recall configuration.

    Returns:
        One of session, user or all.
    """
    scope = getattr(plugin, "memory_recall_scope", "user")
    return scope if scope in ("session", "user", "all") else "user"


def select_automatic_recall_memories(
    memories: list, user_ids: list[str], limit: int
) -> list:
    """Reserve one eligible memory per participant within the automatic budget.

    Args:
        memories: Deduplicated, eligible records in their existing priority order.
            Items may be search dictionaries or cached SessionRecallMemory objects.
        user_ids: Current participants; empty values do not reserve a slot.
        limit: Maximum number of records to retain.

    Returns:
        Selected records in their original relative order. Shared records may
        cover multiple participants, and unused slots follow the existing order.
    """
    if limit <= 0:
        return []
    targets = {str(uid) for uid in user_ids if uid}
    if not targets:
        return memories[:limit]

    selected = set()
    covered = set()
    for index, memory in enumerate(memories):
        metadata = memory_metadata(
            memory if isinstance(memory, dict) else {"metadata": memory.metadata}
        )
        associated = metadata.get("associated_user_ids", [])
        if not isinstance(associated, list):
            continue
        participants = {
            str(uid) for uid in associated if type(uid) in (str, int)
        } & targets
        if participants - covered:
            selected.add(index)
            covered.update(participants)
            if len(selected) >= limit or covered == targets:
                break

    for index in range(len(memories)):
        if len(selected) >= limit:
            break
        selected.add(index)
    return [memory for index, memory in enumerate(memories) if index in selected]
