from datetime import datetime

from ...utils.schemas import MediaCaption
from .base import BaseRepository


class MediaCaptionsRepository(BaseRepository):
    async def insert_media_caption(
        self,
        media_caption: MediaCaption,
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            INSERT INTO media_caption (hash_val, file_name, url, media_type, genre, character, source, text, caption, is_captioned, duration, file_size, query_times, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash_val) DO UPDATE SET
                file_name = excluded.file_name,
                url = excluded.url,
                media_type = excluded.media_type,
                genre = excluded.genre,
                character = excluded.character,
                source = excluded.source,
                text = excluded.text,
                caption = excluded.caption,
                is_captioned = excluded.is_captioned,
                duration = excluded.duration,
                file_size = excluded.file_size,
                updated_at = excluded.updated_at
            """,
            (
                media_caption.hash_val,
                media_caption.file_name,
                media_caption.url,
                media_caption.media_type,
                media_caption.genre,
                media_caption.character,
                media_caption.source,
                media_caption.text,
                media_caption.caption,
                1 if media_caption.is_captioned else 0,
                float(getattr(media_caption, "duration", 0.0) or 0.0),
                int(getattr(media_caption, "file_size", 0) or 0),
                0,
                update_time,
                update_time,
            ),
        )
        await self.conn.commit()

    async def get_media_caption_by_hash(self, hash_val: str) -> MediaCaption | None:
        async with self.conn.execute(
            """
            SELECT hash_val, file_name, url, media_type, genre, character, source, text, caption, is_captioned, duration, file_size, query_times FROM media_caption WHERE hash_val = ?
            """,
            (hash_val,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await self.increment_media_query_times(row["hash_val"])
            row_keys = row.keys() if hasattr(row, "keys") else []
            duration = (
                float(row["duration"])
                if "duration" in row_keys and row["duration"] is not None
                else 0.0
            )
            file_size = (
                int(row["file_size"])
                if "file_size" in row_keys and row["file_size"] is not None
                else 0
            )
            caption = MediaCaption(
                hash_val=row["hash_val"],
                file_name=row["file_name"],
                url=row["url"],
                media_type=row["media_type"],
                genre=row["genre"],
                character=row["character"],
                source=row["source"],
                text=row["text"],
                caption=row["caption"],
                is_captioned=bool(row["is_captioned"]),
                duration=duration,
                file_size=file_size,
            )
            return caption
        return None

    async def get_media_caption_by_filename(
        self, file_name: str
    ) -> MediaCaption | None:
        async with self.conn.execute(
            """
            SELECT hash_val, file_name, url, media_type, genre, character, source, text, caption, is_captioned, duration, file_size, query_times FROM media_caption WHERE file_name = ?
            """,
            (file_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await self.increment_media_query_times(row["hash_val"])
            row_keys = row.keys() if hasattr(row, "keys") else []
            duration = (
                float(row["duration"])
                if "duration" in row_keys and row["duration"] is not None
                else 0.0
            )
            file_size = (
                int(row["file_size"])
                if "file_size" in row_keys and row["file_size"] is not None
                else 0
            )
            caption = MediaCaption(
                hash_val=row["hash_val"],
                file_name=row["file_name"],
                url=row["url"],
                media_type=row["media_type"],
                genre=row["genre"],
                character=row["character"],
                source=row["source"],
                text=row["text"],
                caption=row["caption"],
                is_captioned=bool(row["is_captioned"]),
                duration=duration,
                file_size=file_size,
            )
            return caption
        return None

    async def update_media_caption(
        self,
        media_caption: MediaCaption,
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            UPDATE media_caption
            SET genre = ?, character = ?, source = ?, text = ?, caption = ?, is_captioned = ?, duration = ?, file_size = ?, updated_at = ?
            WHERE hash_val = ?
            """,
            (
                media_caption.genre,
                media_caption.character,
                media_caption.source,
                media_caption.text,
                media_caption.caption,
                1 if media_caption.is_captioned else 0,
                float(getattr(media_caption, "duration", 0.0) or 0.0),
                int(getattr(media_caption, "file_size", 0) or 0),
                update_time,
                media_caption.hash_val,
            ),
        )
        await self.conn.commit()

    async def increment_media_query_times(self, hash_val: str):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            UPDATE media_caption
            SET query_times = query_times + 1, updated_at = ?
            WHERE hash_val = ?
            """,
            (update_time, hash_val),
        )
        await self.conn.commit()

    async def update_media_url(self, hash_val: str, url: str):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            UPDATE media_caption
            SET url = ?, updated_at = ?
            WHERE hash_val = ?
            """,
            (url, update_time, hash_val),
        )
        await self.conn.commit()

    async def clear_media_caption(self):
        await self.conn.execute(
            """
            DELETE FROM media_caption
            """
        )
        await self.conn.commit()
