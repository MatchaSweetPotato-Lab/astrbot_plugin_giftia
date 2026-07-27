import { state } from '../state.js';

export async function loadMedia() {
    const container = document.getElementById("media-list");
    container.innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载转述中...</div>`;

    const params = {
        page: state.pagination.media.page,
        limit: state.pagination.media.limit,
        media_type: document.getElementById("media-type").value,
        search: document.getElementById("media-search").value
    };

    try {
        const res = await window.apiGet("/media", params);
        if (res.status === "success" && res.data) {
            state.pagination.media.total = res.data.total;
            renderMedia(res.data.items);
            window.renderPagination("media-pagination", state.pagination.media, (page) => {
                state.pagination.media.page = page;
                loadMedia();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        container.innerHTML = `<div class="no-data-row flex-grow">加载失败: ${e.message}</div>`;
    }
}

export function isClientPlayableAudio(mimeType) {
    if (!mimeType) return false;

    // 1. 标准 MIME（所有浏览器都支持）— 直接放行
    const standardMimes = [
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/x-flac",
        "audio/mp4",
        "audio/aac",
        "audio/x-m4a",
    ];
    if (standardMimes.includes(mimeType)) return true;

    // 2. 移动端 / IM WebView 通常内置 AMR/Silk 解码器
    const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile/i.test(
        navigator.userAgent || ""
    );
    const isIMWebView = /MicroMessenger|QQ\//i.test(navigator.userAgent || "");

    if (isMobile || isIMWebView) {
        if (
            mimeType === "audio/amr" ||
            mimeType === "audio/silk" ||
            mimeType === "audio/x-amr"
        ) {
            return true;
        }
    }

    return false;
}

export function isPcPlayableAudio(mimeType) {
    return isClientPlayableAudio(mimeType);
}

export function renderAudioUnsupportedNotice(elementId, hash, mimeType) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const wrapper = el.closest(".media-preview-box") || el.parentElement;
    if (!wrapper) return;
    const friendly = mimeType
        ? mimeType.replace("audio/", "").toUpperCase()
        : "未知";
    wrapper.innerHTML = `
        <div class="media-audio-unsupported">
            <div class="media-audio-unsupported-icon">🎧</div>
            <div class="media-audio-unsupported-title">${friendly} 音频</div>
            <div class="media-audio-unsupported-hint">PC 浏览器不支持此格式在线播放<br>（仅移动端 / IM WebView 可播放）</div>
            <a href="#" class="btn btn-secondary btn-small media-audio-download-btn" onclick="window.downloadMedia('${hash}', '${mimeType}'); return false;">
                📥 下载音频
            </a>
        </div>
    `;
}

export async function loadMediaFileB64(hash, elementId, fallbackUrl, type, isThumbnail = false) {
    const el = document.getElementById(elementId);
    if (!el) return;

    try {
        const endpoint = isThumbnail ? `/media/file/thumbnail/b64/${hash}` : `/media/file/b64/${hash}`;
        const res = await window.apiGet(endpoint);
        if (res && res.status === "success" && res.base64) {
            const mimeType = res.content_type || (type === "image" ? "image/jpeg" : (type === "video" ? "video/mp4" : "audio/mpeg"));

            // 音频：若客户端无法播放该格式，才展示不支持提示
            if (type === "audio" || type === "voice") {
                if (!isClientPlayableAudio(mimeType)) {
                    renderAudioUnsupportedNotice(elementId, hash, mimeType);
                    return;
                }
            }

            el.src = `data:${mimeType};base64,${res.base64}`;
        } else {
            if (type === "image") {
                el.onerror = () => {
                    el.onerror = null;
                    el.src = 'placeholder.png';
                };
                el.src = fallbackUrl || 'placeholder.png';
            } else if (type === "audio" || type === "voice") {
                renderAudioUnsupportedNotice(elementId, hash, "");
            } else if (fallbackUrl) {
                el.src = fallbackUrl;
            }
        }
    } catch (e) {
        console.error("Failed to load media base64 for hash:", hash, e);
        if (type === "image") {
            el.onerror = () => {
                el.onerror = null;
                el.src = 'placeholder.png';
            };
            el.src = fallbackUrl || 'placeholder.png';
        } else if (type === "audio" || type === "voice") {
            renderAudioUnsupportedNotice(elementId, hash, "");
        } else if (fallbackUrl) {
            el.src = fallbackUrl;
        }
    }
}

window.loadVideoOnDemand = async function(hash, boxId, fallbackUrlEncoded) {
    const boxEl = document.getElementById(boxId);
    if (!boxEl) return;

    boxEl.onclick = null;
    boxEl.style.cursor = "default";
    boxEl.innerHTML = `<div class="loading-row flex-grow" style="font-size: 12px; padding: 12px;"><span class="loader"></span> 加载视频流中...</div>`;

    try {
        const res = await window.apiGet(`/media/file/b64/${hash}`);
        if (res && res.status === "success" && res.base64) {
            const mimeType = res.content_type || "video/mp4";
            boxEl.innerHTML = `<video controls autoplay preload="metadata" style="width: 100%; height: 100%; object-fit: contain;" src="data:${mimeType};base64,${res.base64}"></video>`;
        } else {
            const fallbackUrl = decodeURIComponent(fallbackUrlEncoded || "");
            if (fallbackUrl && (fallbackUrl.startsWith("http://") || fallbackUrl.startsWith("https://"))) {
                boxEl.innerHTML = `<video controls autoplay preload="metadata" style="width: 100%; height: 100%; object-fit: contain;" src="${fallbackUrl}"></video>`;
            } else {
                boxEl.innerHTML = `<div style="font-size: 11px; color: #888; padding: 8px;">🎬 视频源已过期清理或仅在机器人本地</div>`;
            }
        }
    } catch (e) {
        console.error("加载按需视频失败:", e);
        boxEl.innerHTML = `<div style="font-size: 11px; color: #e74c3c; padding: 8px;">加载失败: ${e.message}</div>`;
    }
};

export function renderMedia(items) {
    const container = document.getElementById("media-list");
    if (!items || items.length === 0) {
        container.innerHTML = `<div class="no-data-row flex-grow">暂无相关媒体转述缓存</div>`;
        return;
    }

    container.innerHTML = items.map(item => {
        let preview = "";
        const uniqueId = `media-preview-${item.hash_val}`;
        if (item.media_type === "image" && item.url) {
            preview = `<img id="${uniqueId}" src="placeholder.png" alt="加载中...">`;
        } else if ((item.media_type === "audio" || item.media_type === "voice") && item.url) {
            preview = `<audio id="${uniqueId}" class="media-audio-player" controls></audio>`;
        } else if (item.media_type === "video") {
            const durStr = item.duration ? window.formatDuration(item.duration) : "";
            const sizeStr = item.file_size ? window.formatFileSize(item.file_size) : "";
            const metaTag = [durStr ? `⏱️ ${durStr}` : "", sizeStr ? `📦 ${sizeStr}` : ""].filter(Boolean).join(" | ");

            preview = `
                <div id="${uniqueId}-box" class="media-video-placeholder-box" style="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; height: 100%; cursor: pointer; text-align: center; padding: 8px; box-sizing: border-box; background: rgba(0,0,0,0.04); transition: background 0.2s;" onclick="window.loadVideoOnDemand('${item.hash_val}', '${uniqueId}-box', '${encodeURIComponent(item.url || '')}')">
                    <div style="font-size: 28px; margin-bottom: 2px;">🎬</div>
                    <div style="font-size: 12px; font-weight: 600; color: var(--font-color); margin-bottom: 2px;">▶️ 点击加载播放</div>
                    <div style="font-size: 11px; color: var(--font-secondary, #777);">${metaTag || '视频缓存'}</div>
                </div>
            `;
        } else {
            preview = `<div style="font-size: 32px;">📄</div>`;
        }

        const encodeMediaArg = value => encodeURIComponent(value || "").replace(/'/g, "%27");
        const encodedCaption = encodeMediaArg(item.caption);
        const encodedUrl = encodeMediaArg(item.url);
        const encodedGenre = encodeMediaArg(item.genre);
        const encodedCharacter = encodeMediaArg(item.character);
        const encodedSource = encodeMediaArg(item.source);
        const encodedText = encodeMediaArg(item.text);

        const badgeClass = item.is_captioned ? "badge-success" : "badge-secondary";
        const badgeText = item.is_captioned ? "已转述" : "待转述";

        return `
            <div class="media-card card">
                <div class="media-preview-box">
                    ${preview}
                    <span class="badge ${badgeClass}" style="position: absolute; top: 8px; right: 8px; z-index: 10;">${badgeText}</span>
                </div>
                <div class="media-info">
                    <div class="media-caption-text">${window.escapeHtml(item.caption || "暂无描述内容")}</div>
                    <div style="margin-top: 8px; font-size: 11px; color: var(--font-secondary);">
                        <strong>风格类型:</strong> ${window.escapeHtml(item.genre || "未知")}
                    </div>
                    <div style="font-size: 11px; color: var(--font-secondary);">
                        <strong>使用次数:</strong> ${item.query_times || 0} 次
                    </div>
                    <div style="font-size: 11px; color: var(--font-secondary);">
                        <strong>转述时间:</strong> ${window.formatDate(item.created_at)}
                    </div>
                </div>
                <div class="media-actions">
                    <button class="btn btn-secondary btn-small" onclick="window.openEditMediaModal('${item.hash_val}', '${encodedUrl}', '${item.media_type}', '${encodedCaption}', '${encodedGenre}', '${encodedCharacter}', '${encodedSource}', '${encodedText}')">修改描述</button>
                    <button class="btn btn-danger btn-small" onclick="window.deleteMedia('${item.hash_val}')">删除缓存</button>
                </div>
            </div>
        `;
    }).join("");

    const hoverTimers = new Map();

    items.forEach(item => {
        if (item.url && (item.media_type === "image" || item.media_type === "audio" || item.media_type === "voice")) {
            const uniqueId = `media-preview-${item.hash_val}`;
            const isImg = item.media_type === "image";
            const shouldLoadThumb = isImg && !state.loadedOriginalMediaG.has(item.hash_val);

            loadMediaFileB64(item.hash_val, uniqueId, item.url, item.media_type, shouldLoadThumb);

            if (isImg) {
                const imgEl = document.getElementById(uniqueId);
                const previewBox = imgEl ? imgEl.closest(".media-preview-box") : null;
                if (previewBox) {
                    previewBox.addEventListener("mouseenter", () => {
                        if (state.loadedOriginalMediaG.has(item.hash_val)) return;

                        const timer = setTimeout(() => {
                            loadMediaFileB64(item.hash_val, uniqueId, item.url, item.media_type, false);
                            state.loadedOriginalMediaG.add(item.hash_val);
                        }, 500);
                        hoverTimers.set(item.hash_val, timer);
                    });

                    previewBox.addEventListener("mouseleave", () => {
                        if (hoverTimers.has(item.hash_val)) {
                            clearTimeout(hoverTimers.get(item.hash_val));
                            hoverTimers.delete(item.hash_val);
                        }
                    });
                }
            }
        }
    });
}
