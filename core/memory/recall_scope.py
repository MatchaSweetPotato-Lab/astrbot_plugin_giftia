"""Shared recall policy for retrieval and cached context."""


def resolve_memory_recall_scope(plugin) -> str:
    """Read the configured scope with the existing user scope as fallback.

    Args:
        plugin: Plugin exposing the memory recall configuration.

    Returns:
        One of session, user or all.
    """
    scope = getattr(plugin, "memory_recall_scope", "user")
    return scope if scope in ("session", "user", "all") else "user"
