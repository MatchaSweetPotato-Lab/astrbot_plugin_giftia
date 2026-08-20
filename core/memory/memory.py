import asyncio
import gc
import threading
import uuid

import lancedb
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from lancedb.pydantic import LanceModel, Vector

from astrbot.api import logger
from astrbot.api.star import Context, StarTools


def get_memory_schema(dim: int):
    class MemorySchema(LanceModel):
        id: str
        bot_name: str
        group_or_user_id: str
        text: str
        vector: Vector(dim)  # type: ignore
        metadata: str = "{}"
        created_at: str
        updated_at: str

    return MemorySchema


class LTM:
    def __init__(self, context: Context, embedding_conf: dict, rerank_conf: dict):
        self.context = context
        self.embedding_conf = embedding_conf
        self.rerank_conf = rerank_conf
        self.db_path = StarTools.get_data_dir("astrbot_plugin_giftia") / "lancedb"
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.table_name = "ltm"

        # 连接数据库 (没有文件夹会自动创建)
        self.db = lancedb.connect(self.db_path)
        self._lazy_initialized = False
        self._lock = threading.Lock()

    @staticmethod
    def _get_provider_id(provider) -> str | None:
        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            provider_id = provider_config.get("id")
            if provider_id:
                return provider_id

        if hasattr(provider, "meta"):
            try:
                return provider.meta().id
            except Exception:
                return None

        return None

    @staticmethod
    def _get_provider_model(provider) -> str:
        for attr_name in ("model_name", "model"):
            model_name = getattr(provider, attr_name, "")
            if model_name:
                return model_name

        if hasattr(provider, "get_model"):
            try:
                model_name = provider.get_model()
                if model_name:
                    return model_name
            except Exception:
                pass

        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            for key in ("rerank_model", "nvidia_rerank_model", "model"):
                model_name = provider_config.get(key)
                if model_name:
                    return model_name

        return "unknown"

    def _get_all_rerank_providers(self) -> list:
        get_all_rerank_providers = getattr(
            self.context, "get_all_rerank_providers", None
        )
        if callable(get_all_rerank_providers):
            return list(get_all_rerank_providers() or [])

        provider_manager = getattr(self.context, "provider_manager", None)
        return list(getattr(provider_manager, "rerank_provider_insts", []) or [])

    def _resolve_rerank_provider(self, provider_id: str):
        rerank_providers = self._get_all_rerank_providers()

        if provider_id:
            for prov in rerank_providers:
                if self._get_provider_id(prov) == provider_id:
                    return prov

            get_provider_by_id = getattr(self.context, "get_provider_by_id", None)
            if callable(get_provider_by_id):
                prov = get_provider_by_id(provider_id)
                if prov and callable(getattr(prov, "rerank", None)):
                    return prov

        if rerank_providers:
            return rerank_providers[0]

        return None

    def _lazy_init(self) -> None:
        """延迟初始化 Embedding 提供商、模型、表结构和重排模型。"""
        if self._lazy_initialized:
            return
        with self._lock:
            if self._lazy_initialized:
                return

            # 1. 自动识别模型与维度
            if self.embedding_conf.get("enabled", False):
                if self.embedding_conf.get("use_external_provider", False):
                    provider_id = self.embedding_conf.get(
                        "external_provider_id", ""
                    ).strip()
                    emb_providers = self.context.get_all_embedding_providers()
                    resolved_provider = None

                    if provider_id:
                        for prov in emb_providers:
                            pid = (
                                prov.provider_config.get("id")
                                if hasattr(prov, "provider_config")
                                else None
                            )
                            if pid == provider_id or (
                                hasattr(prov, "meta") and prov.meta().id == provider_id
                            ):
                                resolved_provider = prov
                                break

                    if not resolved_provider:
                        if provider_id:
                            logger.warning(
                                f"[Giftia LTM] 未找到 ID 为 '{provider_id}' 的 Embedding 提供商，将尝试使用第一个可用的提供商。"
                            )
                        if emb_providers:
                            resolved_provider = emb_providers[0]

                    if not resolved_provider:
                        raise ValueError(
                            "[Giftia LTM] 未在 AstrBot 中找到任何已配置的 Embedding 提供商。请先在 WebUI 的“模型提供商”中添加并启用一个嵌入模型。"
                        )

                    self.embed_provider = resolved_provider
                    self.vector_dim = resolved_provider.get_dim()
                    logger.info(
                        f"LTM当前使用 AstrBot 的外部 Embedding 提供商: {resolved_provider.provider_config.get('id', 'unknown')}，"
                        f"模型名称: {resolved_provider.get_model()}，维度: {self.vector_dim}"
                    )
                else:
                    model_name = self.embedding_conf.get(
                        "model",
                        self.embedding_conf.get("model_name", "BAAI/bge-small-zh-v1.5"),
                    )
                    self.embed_model = TextEmbedding(model_name=model_name)
                    logger.info(
                        f"LTM当前运行本地 FastEmbed 模型: {self.embed_model.model_name}"
                    )
                    self.vector_dim = 512
                    for model_info in TextEmbedding.list_supported_models():
                        if model_info["model"] == self.embed_model.model_name:
                            self.vector_dim = model_info["dim"]
                            logger.info(f"模型官方定义维度: {self.vector_dim}")
                            logger.info(
                                f"模型硬盘空间占用: {model_info['size_in_GB']} GB"
                            )
                            logger.info(f"模型描述: {model_info['description']}")
            else:
                self.vector_dim = 512

            # 2. 动态创建 Pydantic Schema Class
            self.schema_class = get_memory_schema(self.vector_dim)

            # 3. 创建或打开表
            try:
                self.table = self.db.create_table(
                    self.table_name, schema=self.schema_class, exist_ok=True
                )
            except Exception as e:
                if (
                    "schema" in str(e).lower()
                    or "match" in str(e).lower()
                    or "dimension" in str(e).lower()
                ):
                    raise ValueError(
                        f"[Giftia LTM] 向量数据库维度不匹配或 Schema 不兼容！\n"
                        f"错误信息: {e}\n"
                        f"这通常是由于更换了嵌入模型或提供商导致的（不同模型的向量维度不一致，且无法混合使用）。\n"
                        f"请手动删除或备份该文件夹以重新初始化数据库，或者恢复为原先的嵌入模型配置：\n"
                        f"数据库文件夹路径: {self.db_path}"
                    ) from e
                raise

            # 4. 检查已有的向量维度是否匹配 (方案 A)
            try:
                import pyarrow as pa

                arrow_schema = self.table.schema
                vector_field = arrow_schema.field("vector")
                if isinstance(vector_field.type, pa.FixedSizeListType):
                    existing_dim = vector_field.type.list_size
                    if existing_dim != self.vector_dim:
                        raise ValueError(
                            f"[Giftia LTM] 向量数据库维度不匹配！\n"
                            f"当前配置的模型维度为 {self.vector_dim}，而本地已有数据库维度为 {existing_dim}。\n"
                            f"不同模型的向量在数学上不兼容，无法混合使用。\n"
                            f"请手动删除或备份该文件夹以重新初始化数据库，或者恢复为原先的嵌入模型配置：\n"
                            f"数据库文件夹路径: {self.db_path}"
                        )
            except ValueError:
                # 重新抛出维度不兼容的错误
                raise
            except Exception as e:
                logger.warning(
                    f"[Giftia LTM] 检查数据库维度失败（如为新数据库建表则属正常）: {e}"
                )

            if self.rerank_conf.get("enabled", False):
                if self.rerank_conf.get("use_external_provider", False):
                    provider_id = self.rerank_conf.get(
                        "external_provider_id", ""
                    ).strip()
                    resolved_provider = self._resolve_rerank_provider(provider_id)

                    if not resolved_provider:
                        if provider_id:
                            logger.warning(
                                f"[Giftia LTM] 未找到 ID 为 '{provider_id}' 的 Rerank 提供商。"
                            )
                        raise ValueError(
                            "[Giftia LTM] 未在 AstrBot 中找到任何已配置的 Rerank 提供商。请先在 WebUI 的“模型提供商”中添加并启用一个重排序模型。"
                        )

                    resolved_provider_id = self._get_provider_id(resolved_provider)
                    if provider_id and resolved_provider_id != provider_id:
                        logger.warning(
                            f"[Giftia LTM] 未找到 ID 为 '{provider_id}' 的 Rerank 提供商，将尝试使用第一个可用的提供商。"
                        )

                    self.rerank_provider = resolved_provider
                    logger.info(
                        f"LTM当前使用 AstrBot 的外部 Rerank 提供商: {resolved_provider_id or 'unknown'}，"
                        f"模型名称: {self._get_provider_model(resolved_provider)}"
                    )
                else:
                    self.reranker = TextCrossEncoder(
                        model_name=self.rerank_conf.get(
                            "model",
                            self.rerank_conf.get(
                                "model_name", "BAAI/bge-reranker-base"
                            ),
                        )
                    )
                    logger.info(
                        f"LTM当前运行的rerank模型名称: {self.reranker.model_name}"
                    )
                    for model_info in TextCrossEncoder.list_supported_models():
                        if model_info["model"] == self.reranker.model_name:
                            logger.info(
                                f"模型硬盘空间占用: {model_info['size_in_GB']} GB"
                            )
                            logger.info(f"模型描述: {model_info['description']}")

            self._lazy_initialized = True

    def get_all_models(self) -> list[dict]:
        """获取所有支持的模型信息"""
        return TextEmbedding.list_supported_models()

    def get_all_rerank_models(self) -> list[dict]:
        """获取所有支持的模型信息"""
        return TextCrossEncoder.list_supported_models()

    async def add_memory(
        self,
        bot_name: str,
        group_or_user_id: str,
        text: str,
        time: str,
        metadata: str = "{}",
    ) -> tuple[str, bytes] | None:
        self._lazy_init()
        if self.embedding_conf.get("use_external_provider", False):
            if not hasattr(self, "embed_provider"):
                logger.error("Embedding provider not initialized")
                return None
            try:
                vector = await self.embed_provider.get_embedding(text)
            except Exception as e:
                logger.error(f"[Giftia LTM] 获取外部 Embedding 失败: {e}")
                return None

            memory_id = str(uuid.uuid4())
            self.table.add(
                [
                    {
                        "id": memory_id,
                        "bot_name": bot_name,
                        "group_or_user_id": group_or_user_id,
                        "text": text,
                        "vector": vector,
                        "metadata": metadata,
                        "created_at": time,
                        "updated_at": time,
                    }
                ]
            )
            import struct

            vector_bytes = struct.pack(f"{len(vector)}f", *vector)
            return memory_id, vector_bytes
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self._add_memory_sync,
                bot_name,
                group_or_user_id,
                text,
                time,
                metadata,
            )

    def _add_memory_sync(
        self, bot_name: str, group_or_user_id: str, text: str, time: str, metadata: str
    ) -> tuple[str, bytes] | None:
        """添加一条记忆"""
        self._lazy_init()
        if not hasattr(self, "embed_model"):
            logger.error("Embedding model not initialized")
            return None
        memory_id = str(uuid.uuid4())

        # 使用 TextEmbedding 实例手动计算向量
        vector = list(self.embed_model.embed([text]))[0]

        self.table.add(
            [
                {
                    "id": memory_id,
                    "bot_name": bot_name,
                    "group_or_user_id": group_or_user_id,
                    "text": text,
                    "vector": vector.tolist() if hasattr(vector, "tolist") else vector,
                    "metadata": metadata,
                    "created_at": time,
                    "updated_at": time,
                }
            ]
        )
        return memory_id, vector.tobytes()

    async def get_memory(self, memory_ids: list[str]) -> list[dict] | None:
        self._lazy_init()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_memory_sync, memory_ids)

    def _get_memory_sync(self, memory_ids: list[str]) -> list[dict] | None:
        """根据ID获取记忆"""
        self._lazy_init()
        try:
            results = (
                self.table.search()
                .where(f"id IN ({','.join(memory_ids)})")
                .limit(len(memory_ids))
                .to_list()
            )
            if results:
                return results
            return None
        except Exception as e:
            logger.error(f"Get memory failed: {e}")
            return None

    async def search_memory(
        self,
        bot_name: str,
        group_or_user_id: str,
        query: str,
        limit: int = 5,
        threshold: float = 0.7,
    ) -> list[dict]:
        self._lazy_init()
        if self.embedding_conf.get("use_external_provider", False):
            if not hasattr(self, "embed_provider"):
                logger.error("Embedding provider not initialized")
                return []
            try:
                query_vector = await self.embed_provider.get_embedding(query)
            except Exception as e:
                logger.error(f"[Giftia LTM] 外部 Embedding 搜索向量获取失败: {e}")
                return []

            try:
                results = (
                    self.table.search(query_vector)
                    .where(
                        f"bot_name = '{bot_name}' AND group_or_user_id = '{group_or_user_id}'",
                        prefilter=True,
                    )
                    .limit(limit)
                    .to_list()
                )
                if threshold is not None:
                    results = [r for r in results if r.get("_distance", 0) <= threshold]
                return results
            except Exception as e:
                logger.error(f"Search memory failed: {e}")
                return []
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self._search_memory_sync,
                bot_name,
                group_or_user_id,
                query,
                limit,
                threshold,
            )

    def _search_memory_sync(
        self,
        bot_name: str,
        group_or_user_id: str,
        query: str,
        limit: int,
        threshold: float,
    ) -> list[dict]:
        """语义搜索相关记忆"""
        self._lazy_init()
        try:
            if not hasattr(self, "embed_model"):
                logger.error("Embedding model not initialized")
                return []
            # 手动将搜索文本转换为向量再喂给 LanceDB 查询
            query_vector = list(self.embed_model.embed([query]))[0]
            if hasattr(query_vector, "tolist"):
                query_vector = query_vector.tolist()

            results = (
                self.table.search(query_vector)
                .where(
                    f"bot_name = '{bot_name}' AND group_or_user_id = '{group_or_user_id}'",
                    prefilter=True,
                )
                .limit(limit)
                .to_list()
            )
            # 过滤低相关性条目 (LanceDB 的 _distance 越小代表越相似)
            # logger.debug(f"Memory search results: {results}")
            if threshold is not None:
                results = [r for r in results if r.get("_distance", 0) <= threshold]
            return results
        except Exception as e:
            logger.error(f"Search memory failed: {e}")
            return []

    async def get_all_memories(
        self, bot_name: str, group_or_user_id: str, limit: int = 100
    ) -> list[dict]:
        self._lazy_init()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._get_all_memories_sync, bot_name, group_or_user_id, limit
        )

    def _get_all_memories_sync(
        self, bot_name: str, group_or_user_id: str, limit: int
    ) -> list[dict]:
        """获取所有早期记忆"""
        self._lazy_init()
        try:
            results = (
                self.table.search()
                .where(
                    f"bot_name = '{bot_name}' AND group_or_user_id = '{group_or_user_id}'"
                )
                .limit(limit)
                .to_list()
            )
            return results
        except Exception as e:
            logger.error(f"Get all memories failed: {e}")
            return []

    async def delete_memory(self, memory_id: str) -> bool:
        self._lazy_init()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._delete_memory_sync, memory_id)

    def _delete_memory_sync(self, memory_id: str) -> bool:
        """删除一条记忆"""
        self._lazy_init()
        try:
            self.table.delete(f"id = '{memory_id}'")
            return True
        except Exception as e:
            logger.error(f"Delete memory failed: {e}")
            return False

    async def delete_all_memories(self, bot_name: str, group_or_user_id: str) -> bool:
        """删除全部记忆"""
        self._lazy_init()
        try:
            self.table.delete(
                f"bot_name = '{bot_name}' AND group_or_user_id = '{group_or_user_id}'"
            )
            return True
        except Exception as e:
            logger.error(f"Delete all memories failed: {e}")
            return False

    async def rerank_memories(
        self, query: str, memories: list[dict], top_k: int = 5, threshold: float = 0.5
    ) -> list[dict]:
        """
        使用重排模型或 AstrBot Rerank 提供商对记忆进行重排序
        :param query: 用户的查询语句
        :param memories: 记忆列表，每个元素包含 'text' 字段
        :param top_k: 返回前 k 个最相关的记忆
        :return: 按相关性排序后的记忆列表
        """
        self._lazy_init()
        try:
            memory_texts = [memory["text"] for memory in memories]

            if self.rerank_conf.get("use_external_provider", False):
                if not hasattr(self, "rerank_provider"):
                    logger.error("Rerank provider not initialized")
                    return []

                rerank_results = await self.rerank_provider.rerank(
                    query, memory_texts, top_n=top_k
                )

                reranked_memories = []
                for result in rerank_results:
                    idx = getattr(result, "index", None)
                    if idx is None or idx < 0 or idx >= len(memories):
                        logger.warning(f"[Giftia LTM] Rerank 返回了无效索引: {idx}")
                        continue

                    score = float(getattr(result, "relevance_score", 0.0))
                    if score < threshold:
                        continue

                    memory = memories[idx]
                    memory["score"] = score
                    reranked_memories.append(memory)

                reranked_memories.sort(key=lambda x: x["score"], reverse=True)
                return reranked_memories[:top_k]

            if not hasattr(self, "reranker"):
                logger.error("Reranker not initialized")
                return []

            # 使用 Cross-Encoder 计算相关性分数
            # reranker.rerank 返回的是一个生成器，包含每个记忆的分数
            scores_gen = self.reranker.rerank(query, memory_texts)
            scores = list(scores_gen)

            # 将分数与记忆合并，并按分数从高到低排序
            reranked_memories = []
            for score, memory in zip(scores, memories):
                if score < threshold:
                    continue
                memory["score"] = score
                reranked_memories.append(memory)

            # 按分数排序（降序）
            reranked_memories.sort(key=lambda x: x["score"], reverse=True)

            # 返回前 top_k 个结果
            return reranked_memories[:top_k]

        except Exception as e:
            logger.error(f"Rerank memories failed: {e}")
            # 如果失败，返回原始记忆（不排序）
            return memories

    def close(self):
        """显式释放模型底层 ONNX 内存"""
        logger.info("正在释放 LTM 模型内存...")
        if hasattr(self, "embed_model"):
            del self.embed_model
        if hasattr(self, "reranker"):
            del self.reranker

        # 强制进行 Python 垃圾回收，促使底层 C++ 对象析构
        gc.collect()
