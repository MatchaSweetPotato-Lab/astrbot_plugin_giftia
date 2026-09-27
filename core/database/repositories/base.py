import aiosqlite


class BaseRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
