from __future__ import annotations

import os
import uuid
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


from ..bot.bot_config_manager import DEFAULT_INTERACTIVE_FEATURES
from .web_helpers import read_file_to_base64, safe_path_join


class BotApi:
    """Bot management web APIs for Giftia Dashboard."""

    def __init__(self, giftia):
        self.giftia = giftia

    def _extract_providers_from_config(self, context, llm_providers: list[dict], tts_providers: list[dict]) -> None:

        """从 AstrBot 基础配置 JSON/Config 中解析已注册的 LLM 与 TTS 供应商。"""
        cfg = getattr(context, "config", None) or getattr(context, "_config", None)
        if not cfg and hasattr(context, "astrbot_config_mgr"):
            acm = getattr(context, "astrbot_config_mgr")
            cfg = getattr(acm, "default_conf", None) or getattr(acm, "conf", None) or getattr(acm, "config", None)

        if not cfg:
            return

        getter = cfg.get if hasattr(cfg, "get") and callable(cfg.get) else (lambda k, d=None: getattr(cfg, k, d))

        # 专有语音/TTS 供应商配置项
        for tts_key in ["speech_providers", "tts_providers", "text_to_speech_providers", "speech"]:
            t_list = getter(tts_key, [])
            items = t_list if isinstance(t_list, list) else (list(t_list.values()) if isinstance(t_list, dict) else [])
            for item in items:
                if isinstance(item, dict):
                    p_id = str(item.get("id") or item.get("name") or "").strip()
                    p_type = str(item.get("type") or "").strip()
                    p_name = str(item.get("name") or p_id).strip()
                    if p_id and not any(x["id"] == p_id for x in tts_providers):
                        tts_providers.append({"id": p_id, "name": p_name or p_id, "type": p_type})

        # 通用大模型/提供商配置项 ("providers")
        for prov_key in ["providers", "provider", "model_providers"]:
            p_list = getter(prov_key, [])
            items = p_list if isinstance(p_list, list) else (list(p_list.values()) if isinstance(p_list, dict) else [])
            for item in items:
                if isinstance(item, dict):
                    p_id = str(item.get("id") or item.get("name") or "").strip()
                    p_type = str(item.get("type") or "").strip()
                    p_name = str(item.get("name") or p_id).strip()
                    if p_id:
                        info = {"id": p_id, "name": p_name or p_id, "type": p_type}
                        if any(k in p_type.lower() for k in ["tts", "speech", "voice", "fishaudio", "gsv", "cosy"]):
                            if not any(x["id"] == p_id for x in tts_providers):
                                tts_providers.append(info)
                        else:
                            if not any(k in p_type.lower() for k in ["asr", "embedding", "embed", "rerank"]):
                                if not any(x["id"] == p_id for x in llm_providers):
                                    llm_providers.append(info)

    def _extract_providers_from_runtime(self, context, llm_providers: list[dict], tts_providers: list[dict]) -> None:
        """从 ProviderManager 运行时实例回退解析已加载的提供商。"""
        pm = getattr(context, "provider_manager", None)
        all_prov_sources = []
        if pm:
            for attr_name in ["providers", "_providers", "provider_insts", "llm_providers"]:
                provs = getattr(pm, attr_name, None)
                if isinstance(provs, dict):
                    all_prov_sources.extend(list(provs.values()))
                elif isinstance(provs, (list, tuple)):
                    all_prov_sources.extend(list(provs))

            for meth_name in ["get_providers", "get_using_providers", "get_all_providers"]:
                if hasattr(pm, meth_name) and callable(getattr(pm, meth_name)):
                    try:
                        res = getattr(pm, meth_name)()
                        if res:
                            all_prov_sources.extend(list(res.values()) if isinstance(res, dict) else list(res))
                    except Exception:
                        pass

        if hasattr(context, "get_all_providers") and callable(context.get_all_providers):
            try:
                res = context.get_all_providers()
                if res:
                    all_prov_sources.extend(list(res.values()) if isinstance(res, dict) else list(res))
            except Exception:
                pass

        for item in all_prov_sources:
            if not item:
                continue
            p_id, p_type, p_name = "", "", ""
            if isinstance(item, dict):
                p_id = str(item.get("id") or item.get("name") or "").strip()
                p_type = str(item.get("type") or "").strip()
                p_name = str(item.get("name") or p_id).strip()
            else:
                try:
                    if hasattr(item, "meta") and callable(item.meta):
                        m = item.meta()
                        p_id = str(getattr(m, "id", "") or "")
                        p_type = str(getattr(m, "type", "") or "")
                        p_name = str(getattr(m, "name", "") or p_id)
                except Exception:
                    pass
                if not p_id:
                    p_id = str(getattr(item, "id", "") or getattr(item, "provider_id", "") or "").strip()
                if not p_type:
                    p_type = str(getattr(item, "type", "") or type(item).__name__).strip()
                if not p_name:
                    p_name = str(getattr(item, "name", "") or p_id).strip()

            if p_id:
                info = {"id": p_id, "name": p_name or p_id, "type": p_type}
                if any(k in p_type.lower() for k in ["tts", "speech", "voice", "fishaudio", "gsv", "cosy"]):
                    if not any(x["id"] == p_id for x in tts_providers):
                        tts_providers.append(info)
                else:
                    if not any(k in p_type.lower() for k in ["asr", "embedding", "embed", "rerank"]):
                        if not any(x["id"] == p_id for x in llm_providers):
                            llm_providers.append(info)

    def _extract_platform_adapters(self, context) -> list[dict]:
        """从 PlatformManager 提取活跃及配置的消息平台适配器 ID 列表。"""
        adapters = []
        try:
            pm_plat = getattr(context, "platform_manager", None)
            if pm_plat:
                # 1. 从运行中的适配器实例提取 (支持 inst.meta() 与 inst.metadata)
                insts = getattr(pm_plat, "get_insts", lambda: [])() or getattr(pm_plat, "platform_insts", []) or []
                for inst in insts:
                    meta_raw = getattr(inst, "meta", None) or getattr(inst, "metadata", None)
                    if callable(meta_raw):
                        try:
                            meta = meta_raw()
                        except Exception:
                            meta = None
                    else:
                        meta = meta_raw

                    if meta:
                        a_id = str(getattr(meta, "id", "") or "").strip()
                        if a_id and not any(x["id"] == a_id for x in adapters):
                            adapters.append({
                                "id": a_id,
                                "name": str(getattr(meta, "name", a_id) or a_id),
                                "platform_name": str(getattr(meta, "platform_name", "")),
                            })

                # 2. 回退检查：从 platforms_config 中补充提取在配置中启用或存在的平台
                platforms_config = getattr(pm_plat, "platforms_config", None) or []
                for p_cfg in platforms_config:
                    if isinstance(p_cfg, dict):
                        a_id = str(p_cfg.get("id", "") or "").strip()
                        if a_id and not any(x["id"] == a_id for x in adapters):
                            adapters.append({
                                "id": a_id,
                                "name": str(p_cfg.get("name") or a_id),
                                "platform_name": str(p_cfg.get("type", "")),
                            })
        except Exception as e:
            logger.warning(f"[Giftia BotApi] Fetching adapters failed: {e}")
        return adapters

    def _get_available_metadata(self) -> dict:

        """汇总可用的 LLM 提供商、TTS 提供商、消息适配器列表和内置交互功能。"""
        llm_providers = []
        tts_providers = []
        adapters = []

        giftia = getattr(self, "giftia", None)
        context = getattr(giftia, "context", None) if giftia else None

        if context:
            self._extract_providers_from_config(context, llm_providers, tts_providers)
            self._extract_providers_from_runtime(context, llm_providers, tts_providers)
            adapters = self._extract_platform_adapters(context)

        logger.info(f"[Giftia BotApi] Metadata fetched: {len(llm_providers)} LLM providers, {len(tts_providers)} TTS providers, {len(adapters)} adapters")
        return {
            "llm_providers": llm_providers,
            "tts_providers": tts_providers,
            "adapters": adapters,
            "interactive_features": DEFAULT_INTERACTIVE_FEATURES,
        }


    async def get_bots(self):
        """Get all bot configurations and system metadata."""
        try:
            giftia = getattr(self, "giftia", None)
            if not giftia or not hasattr(giftia, "bot_config_manager"):
                return error_response("BotConfigManager 未初始化")

            bots = giftia.bot_config_manager.load_bots()
            metadata = self._get_available_metadata()

            return json_response({
                "status": "success",
                "data": {
                    "bots": bots,
                    "metadata": metadata,
                }
            })
        except Exception as e:
            logger.error(f"[Giftia API] get_bots error: {e}", exc_info=True)
            return error_response(f"获取机器人列表失败: {str(e)}")

    async def save_bot(self):
        """Create or update a bot configuration."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            bot_name = str(body.get("name") or "").strip()
            if not bot_name:
                return error_response("机器人名称 (name) 不能为空")

            giftia = getattr(self, "giftia", None)
            if not giftia or not hasattr(giftia, "bot_config_manager"):
                return error_response("BotConfigManager 未初始化")

            manager = giftia.bot_config_manager
            existing_bots = manager.load_bots()

            normalized_bot = manager.normalize_bot_config(body)
            original_name = str(body.get("_original_name") or bot_name).strip()

            # Target update vs insert
            updated = False
            new_bots = []
            for b in existing_bots:
                if b["name"] == original_name or b["name"] == bot_name:
                    new_bots.append(normalized_bot)
                    updated = True
                else:
                    new_bots.append(b)

            if not updated:
                new_bots.append(normalized_bot)

            if manager.save_bots(new_bots):
                giftia.sync_bot_maps()
                return json_response({"status": "success", "message": "机器人配置保存成功", "bot": normalized_bot})
            else:
                return error_response("保存机器人配置到文件失败")
        except Exception as e:
            logger.error(f"[Giftia API] save_bot error: {e}", exc_info=True)
            return error_response(f"保存机器人配置失败: {str(e)}")

    async def delete_bot(self):
        """Delete a bot configuration by name."""
        try:
            body = await request.json()
            bot_name = str(body.get("name") or "").strip()
            if not bot_name:
                return error_response("缺少 name 参数")

            giftia = getattr(self, "giftia", None)
            if not giftia or not hasattr(giftia, "bot_config_manager"):
                return error_response("BotConfigManager 未初始化")

            manager = giftia.bot_config_manager
            existing_bots = manager.load_bots()
            new_bots = [b for b in existing_bots if b["name"] != bot_name]

            if len(new_bots) == len(existing_bots):
                return error_response(f"未找到名称为 '{bot_name}' 的机器人")

            if manager.save_bots(new_bots):
                giftia.sync_bot_maps()
                return json_response({"status": "success", "message": f"机器人 '{bot_name}' 已成功删除"})
            else:
                return error_response("保存配置失败")
        except Exception as e:
            logger.error(f"[Giftia API] delete_bot error: {e}", exc_info=True)
            return error_response(f"删除机器人失败: {str(e)}")

    async def toggle_bot(self):
        """Quick toggle enable/disable status for a bot."""
        try:
            body = await request.json()
            bot_name = str(body.get("name") or "").strip()
            enabled = bool(body.get("enabled", True))
            if not bot_name:
                return error_response("缺少 name 参数")

            giftia = getattr(self, "giftia", None)
            if not giftia or not hasattr(giftia, "bot_config_manager"):
                return error_response("BotConfigManager 未初始化")

            manager = giftia.bot_config_manager
            existing_bots = manager.load_bots()
            found = False
            for b in existing_bots:
                if b["name"] == bot_name:
                    b["enabled"] = enabled
                    found = True
                    break

            if not found:
                return error_response(f"未找到名称为 '{bot_name}' 的机器人")

            if manager.save_bots(existing_bots):
                giftia.sync_bot_maps()
                return json_response({"status": "success", "message": f"机器人 '{bot_name}' 状态已更新", "enabled": enabled})
            else:
                return error_response("保存配置失败")
        except Exception as e:
            logger.error(f"[Giftia API] toggle_bot error: {e}", exc_info=True)
            return error_response(f"开关机器人状态失败: {str(e)}")

    async def upload_signature_voice(self):
        """Upload audio file for signature voice replacement."""
        try:
            giftia = getattr(self, "giftia", None)
            filename = ""
            file_bytes = None

            # Safe check request.files
            if hasattr(request, "files"):
                req_files = getattr(request, "files")
                files_dict = None
                if callable(req_files):
                    try:
                        files_dict = req_files()
                    except Exception:
                        files_dict = None
                elif isinstance(req_files, dict) or hasattr(req_files, "items"):
                    files_dict = req_files

                if files_dict and hasattr(files_dict, "items"):
                    for key, file_item in files_dict.items():
                        filename = getattr(file_item, "filename", "") or getattr(file_item, "name", "") or "voice.wav"
                        file_bytes = getattr(file_item, "body", None) or getattr(file_item, "content", None) or getattr(file_item, "read", lambda: None)()
                        if file_bytes:
                            break

            if not file_bytes:
                body = await request.json()
                if isinstance(body, dict):
                    filename = str(body.get("filename") or "voice.wav")
                    b64_content = str(body.get("content") or "")
                    if b64_content:
                        import base64
                        if "," in b64_content:
                            b64_content = b64_content.split(",", 1)[1]
                        file_bytes = base64.b64decode(b64_content)

            if not file_bytes:
                return error_response("未接收到有效的语音文件内容")

            ext = os.path.splitext(filename)[1] or ".wav"
            data_dir = str(giftia.bot_config_manager.data_dir) if (giftia and hasattr(giftia, "bot_config_manager")) else "."
            voices_dir = os.path.join(data_dir, "voices")
            os.makedirs(voices_dir, exist_ok=True)

            clean_name = os.path.basename(filename) or f"voice_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(voices_dir, clean_name)

            with open(save_path, "wb") as f:
                f.write(file_bytes)

            rel_path = os.path.relpath(save_path, data_dir)
            return json_response({
                "status": "success",
                "message": "语音文件上传成功",
                "data": {
                    "abs_path": save_path,
                    "rel_path": rel_path.replace("\\", "/"),
                    "filename": clean_name,
                }
            })
        except Exception as e:
            logger.error(f"[Giftia API] upload_signature_voice error: {e}", exc_info=True)
            return error_response(f"上传语音文件失败: {str(e)}")

    async def list_signature_voices(self):
        """List all uploaded voice files in data/voices directory."""
        try:
            giftia = getattr(self, "giftia", None)
            data_dir = str(giftia.bot_config_manager.data_dir) if (giftia and hasattr(giftia, "bot_config_manager")) else "."
            voices_dir = os.path.join(data_dir, "voices")
            os.makedirs(voices_dir, exist_ok=True)

            files = []
            valid_exts = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
            for f in os.listdir(voices_dir):
                ext = os.path.splitext(f)[1].lower()
                if ext in valid_exts:
                    full_path = os.path.join(voices_dir, f)
                    rel_path = os.path.relpath(full_path, data_dir).replace("\\", "/")
                    files.append({
                        "filename": f,
                        "rel_path": rel_path,
                        "size": os.path.getsize(full_path),
                    })

            files.sort(key=lambda x: x["filename"])
            return json_response({"status": "success", "data": files})
        except Exception as e:
            logger.error(f"[Giftia API] list_signature_voices error: {e}", exc_info=True)
            return error_response(f"获取语音列表失败: {str(e)}")

    def _resolve_voice_file_path(self, rel_path: str) -> str | None:
        """Safely resolve voice file path ensuring it stays strictly inside data/voices directory."""
        if not rel_path or not isinstance(rel_path, str):
            return None

        giftia = getattr(self, "giftia", None)
        data_dir = str(giftia.bot_config_manager.data_dir) if (giftia and hasattr(giftia, "bot_config_manager")) else "."
        voices_dir = os.path.abspath(os.path.join(data_dir, "voices"))

        cleaned_rel = rel_path.strip().lstrip("/\\")
        # 若传入路径带有 "voices/" 前缀，先截取掉前缀以归一化
        if cleaned_rel.lower().startswith("voices/") or cleaned_rel.lower().startswith("voices\\"):
            cleaned_rel = cleaned_rel[7:].lstrip("/\\")

        # 1. 统一以 voices_dir 作为唯一基准目录调用 safe_path_join 解析
        target = safe_path_join(voices_dir, cleaned_rel)
        if target and os.path.isfile(target):
            return target

        # 2. 纯文件名降级尝试
        basename = os.path.basename(cleaned_rel)
        if basename and basename != cleaned_rel:
            target = safe_path_join(voices_dir, basename)
            if target and os.path.isfile(target):
                return target

        return None


    async def get_voice_file_b64(self):
        """Get Base64 data URL for a voice audio file for instant browser playback."""
        try:
            body = await request.json()
            rel_path = str(body.get("rel_path") or body.get("filename") or "").strip()
            if not rel_path:
                return error_response("缺少 rel_path 参数")

            full_path = self._resolve_voice_file_path(rel_path)
            if not full_path:
                return error_response(f"语音文件不存在或目录受限: {rel_path}")

            b64_str, mime_type = read_file_to_base64(full_path, fallback_mime="audio/wav")
            data_url = f"data:{mime_type};base64,{b64_str}"

            return json_response({
                "status": "success",
                "base64": b64_str,
                "content_type": mime_type,
                "data": {
                    "b64": data_url,
                    "base64": b64_str,
                    "content_type": mime_type,
                }
            })
        except Exception as e:
            logger.error(f"[Giftia API] get_voice_file_b64 error: {e}", exc_info=True)
            return error_response(f"获取语音文件失败: {str(e)}")

    async def delete_signature_voice(self):
        """Delete an uploaded voice file from data/voices directory."""
        try:
            body = await request.json()
            rel_path = str(body.get("rel_path") or body.get("filename") or "").strip()
            if not rel_path:
                return error_response("缺少 rel_path 参数")

            full_path = self._resolve_voice_file_path(rel_path)
            if not full_path:
                return error_response(f"语音文件不存在或目录受限: {rel_path}")

            os.remove(full_path)
            return json_response({"status": "success", "message": f"语音文件 '{os.path.basename(full_path)}' 已成功删除"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_signature_voice error: {e}", exc_info=True)
            return error_response(f"删除语音文件失败: {str(e)}")

