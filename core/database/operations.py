import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import wraps
from weakref import WeakKeyDictionary

import aiosqlite

_locks = WeakKeyDictionary()
_owners = WeakKeyDictionary()


@asynccontextmanager
async def database_operation(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Keep cursors and transactions on a shared connection within one task.

    Args:
        conn: Connection shared by repositories and direct SQL callers.

    Yields:
        Control while the current task exclusively owns the connection.

    Raises:
        BaseException: The operation failed or was cancelled, after rollback.
    """
    task = asyncio.current_task()
    if _owners.get(conn) is task:
        yield
        return

    lock = _locks.setdefault(conn, asyncio.Lock())
    async with lock:
        _owners[conn] = task
        try:
            yield
        except BaseException:
            await conn.rollback()
            raise
        finally:
            _owners.pop(conn, None)


def serialized_operation(method):
    """Serialize a store method with every other user of its connection.

    Args:
        method: Async repository or profile-store method using ``self.conn``.

    Returns:
        The method with connection ownership and failure rollback.
    """

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with database_operation(self.conn):
            return await method(self, *args, **kwargs)

    return wrapped
