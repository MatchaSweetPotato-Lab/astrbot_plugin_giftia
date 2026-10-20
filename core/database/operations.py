import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import wraps
from weakref import WeakKeyDictionary

import aiosqlite
from aiosqlite.context import Result

_locks = WeakKeyDictionary()
_owners = WeakKeyDictionary()


class GuardedQuery(Result):
    """Join connection ownership for context-managed execute calls.

    Awaited statements retain aiosqlite's cursor and commit behavior. Their
    enclosing repository operation or transaction owns the connection instead.
    """

    def __init__(self, conn: aiosqlite.Connection, result: Result):
        super().__init__(result)
        self._connection = conn
        self._result = result
        self._exit_stack = AsyncExitStack()

    async def __aenter__(self) -> aiosqlite.Cursor:
        """Own the connection until the cursor has been closed.

        Returns:
            The original cursor from aiosqlite.
        """
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(database_operation(self._connection))
                cursor = await stack.enter_async_context(self._result)
                self._exit_stack = stack.pop_all()
                return cursor
        except BaseException:
            # Cancellation can happen before the query coroutine is awaited.
            self._result.close()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        """Close the cursor before rollback and connection ownership release.

        Args:
            exc_type: Exception type raised by the query scope, if any.
            exc: Exception raised by the query scope, if any.
            tb: Traceback associated with the exception.

        Returns:
            Whether an entered context suppressed the exception.
        """
        return await self._exit_stack.__aexit__(exc_type, exc, tb)


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
