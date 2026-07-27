// Giftia Dashboard Modals - Media Actions & Cache Clean

// 1. Edit Media Caption
window.openEditMediaModal = function(hash, urlEncoded, type, captionEncoded, genreEncoded, characterEncoded, sourceEncoded, textEncoded) {
    const url = decodeURIComponent(urlEncoded);
    const caption = decodeURIComponent(captionEncoded);
    const genre = decodeURIComponent(genreEncoded || "");
    const character = decodeURIComponent(characterEncoded || "");
    const source = decodeURIComponent(sourceEncoded || "");
    const text = decodeURIComponent(textEncoded || "");
    
    document.getElementById("edit-media-hash").value = hash;
    document.getElementById("edit-media-caption").value = caption;
    document.getElementById("edit-media-genre").value = genre;
    document.getElementById("edit-media-character").value = character;
    document.getElementById("edit-media-source").value = source;
    document.getElementById("edit-media-text").value = text;

    const titleEl = document.getElementById("edit-media-title");
    if (titleEl) {
        titleEl.textContent = `修改媒体转述描述 (${hash})`;
    }

    const tabsContainer = document.querySelector(".edit-media-tabs");
    if (tabsContainer) {
        tabsContainer.querySelectorAll(".media-tab-btn").forEach(b => {
            if (b.getAttribute("data-mediatab") === "caption") {
                b.classList.add("active");
            } else {
                b.classList.remove("active");
            }
        });
        tabsContainer.querySelectorAll(".media-tab-panel").forEach(p => {
            if (p.id === "mediatab-caption") {
                p.classList.add("active");
            } else {
                p.classList.remove("active");
            }
        });
    }
    
    const previewContainer = document.getElementById("edit-media-preview");
    const gridElId = `media-preview-${hash}`;
    const gridEl = document.getElementById(gridElId);
    const isOriginalLoaded = (type === "image" && window.GiftiaApp.loadedOriginalMediaG && window.GiftiaApp.loadedOriginalMediaG.has(hash)) || 
                             (type !== "image" && gridEl && gridEl.src && gridEl.src.startsWith("data:"));

    if (isOriginalLoaded && gridEl && gridEl.src && gridEl.src.startsWith("data:")) {
        if (type === "image" && url) {
            const uniqueId = `edit-media-preview-img-${hash}`;
            previewContainer.innerHTML = `<img id="${uniqueId}" src="${gridEl.src}">`;
        } else if ((type === "audio" || type === "voice") && url) {
            const uniqueId = `edit-media-preview-audio-${hash}`;
            const dataUrl = gridEl.src;
            const mimeMatch = dataUrl.match(/^data:([^;,]+)/);
            const mimeType = mimeMatch ? mimeMatch[1] : "";
            if (window.GiftiaApp.isClientPlayableAudio(mimeType)) {
                previewContainer.innerHTML = `<audio id="${uniqueId}" src="${dataUrl}" controls></audio>`;
            } else {
                const friendly = mimeType ? mimeType.replace("audio/", "").toUpperCase() : "未知";
                previewContainer.innerHTML = `
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
        } else if (type === "video") {
            const uniqueId = `edit-media-preview-video-${hash}`;
            previewContainer.innerHTML = `
                <div id="${uniqueId}-box" class="media-video-placeholder-box" style="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; height: 100%; min-height: 140px; cursor: pointer; text-align: center; padding: 12px; box-sizing: border-box; background: rgba(0,0,0,0.04); border-radius: 6px;" onclick="window.loadVideoOnDemand('${hash}', '${uniqueId}-box', '${encodeURIComponent(url || '')}')">
                    <div style="font-size: 28px; margin-bottom: 2px;">🎬</div>
                    <div style="font-size: 12px; font-weight: 600; color: var(--font-color);">▶️ 点击加载/播放视频</div>
                </div>
            `;
        } else {
            previewContainer.innerHTML = `<div style="font-size: 24px;">📄</div>`;
        }
    } else {
        if (type === "image" && url) {
            const uniqueId = `edit-media-preview-img-${hash}`;
            previewContainer.innerHTML = `<img id="${uniqueId}" src="placeholder.png" alt="加载中...">`;
            window.GiftiaApp.loadMediaFileB64(hash, uniqueId, url, type, false);
            
            if (gridEl && window.GiftiaApp.loadedOriginalMediaG) {
                if (!window.GiftiaApp.loadedOriginalMediaG.has(hash)) {
                    window.GiftiaApp.loadMediaFileB64(hash, gridElId, url, type, false);
                    window.GiftiaApp.loadedOriginalMediaG.add(hash);
                }
            }
        } else if ((type === "audio" || type === "voice") && url) {
            const uniqueId = `edit-media-preview-audio-${hash}`;
            previewContainer.innerHTML = `<audio id="${uniqueId}" controls></audio>`;
            window.GiftiaApp.loadMediaFileB64(hash, uniqueId, url, type);
        } else if (type === "video") {
            const uniqueId = `edit-media-preview-video-${hash}`;
            previewContainer.innerHTML = `
                <div id="${uniqueId}-box" class="media-video-placeholder-box" style="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; height: 100%; min-height: 140px; cursor: pointer; text-align: center; padding: 12px; box-sizing: border-box; background: rgba(0,0,0,0.04); border-radius: 6px;" onclick="window.loadVideoOnDemand('${hash}', '${uniqueId}-box', '${encodeURIComponent(url || '')}')">
                    <div style="font-size: 28px; margin-bottom: 2px;">🎬</div>
                    <div style="font-size: 12px; font-weight: 600; color: var(--font-color);">▶️ 点击加载/播放视频</div>
                </div>
            `;
        } else {
            previewContainer.innerHTML = `<div style="font-size: 24px;">📄</div>`;
        }
    }
    
    window.openModal("edit-media-modal");
};

window.submitEditMedia = async function() {
    const hash = document.getElementById("edit-media-hash").value;
    const caption = document.getElementById("edit-media-caption").value.trim();
    const genre = document.getElementById("edit-media-genre").value.trim();
    const character = document.getElementById("edit-media-character").value.trim();
    const source = document.getElementById("edit-media-source").value.trim();
    const text = document.getElementById("edit-media-text").value.trim();

    if (!caption) {
        window.showToast("描述内容不能为空！");
        return;
    }

    try {
        const res = await window.apiPost("/media/update", {
            hash_val: hash,
            caption: caption,
            genre: genre,
            character: character,
            source: source,
            text: text
        });
        if (res.status === "success") {
            window.showToast("更新成功！");
            window.closeModal("edit-media-modal");
            window.GiftiaApp.loadMedia();
        } else {
            window.showToast(`保存失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

// 2. Delete Media Caption
window.deleteMedia = function(hash) {
    window.showConfirm("确认清理媒体缓存", "确定要清理这条媒体缓存吗？这将清空它的大模型文字转述内容。", async () => {
        try {
            const res = await window.apiPost("/media/delete", { hash_val: hash });
            if (res.status === "success") {
                window.showToast("媒体描述已清理");
                window.GiftiaApp.loadMedia();
            } else {
                window.showToast(`清理失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

// 3. Cache Cleanup Modal
window.openCleanCacheModal = async function() {
    window.openModal("clean-cache-modal");
    const container = document.getElementById("clean-media-genre-container");
    container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">加载中...</div>';
    
    document.getElementById("clean-media-type").value = "all";
    document.getElementById("clean-max-query-times").value = "0";
    document.getElementById("clean-genre-exclude").checked = false;
    document.getElementById("clean-cache-preview-info").innerHTML = '点击下方“计算清理空间”进行预估...';
    document.getElementById("clean-cache-preview-info").style.borderLeftColor = "var(--border-color)";

    try {
        const res = await window.apiGet("/media/genres");
        if (res && res.status === "success" && res.genres) {
            container.innerHTML = "";
            
            const unspecifiedDiv = document.createElement("div");
            unspecifiedDiv.style.display = "flex";
            unspecifiedDiv.style.alignItems = "center";
            unspecifiedDiv.style.gap = "6px";
            unspecifiedDiv.style.margin = "4px 0";
            unspecifiedDiv.innerHTML = `
                <input type="checkbox" id="clean-genre-unspecified" value="" style="width: auto; margin: 0; cursor: pointer;" checked>
                <label for="clean-genre-unspecified" style="margin: 0; cursor: pointer; font-weight: normal; color: var(--font-color);">[未指定风格]</label>
            `;
            container.appendChild(unspecifiedDiv);

            res.genres.forEach((genre, idx) => {
                const genreDiv = document.createElement("div");
                genreDiv.style.display = "flex";
                genreDiv.style.alignItems = "center";
                genreDiv.style.gap = "6px";
                genreDiv.style.margin = "4px 0";
                genreDiv.innerHTML = `
                    <input type="checkbox" name="clean-genre-checkbox" id="clean-genre-chk-${idx}" value="${window.escapeHtml(genre)}" style="width: auto; margin: 0; cursor: pointer;" checked>
                    <label for="clean-genre-chk-${idx}" style="margin: 0; cursor: pointer; font-weight: normal; color: var(--font-color);">${window.escapeHtml(genre)}</label>
                `;
                container.appendChild(genreDiv);
            });
        } else {
            container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">暂无可用风格，或加载失败。</div>';
        }
    } catch (e) {
        console.error("Failed to load genres for cleanup modal:", e);
        container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">加载风格列表出错。</div>';
    }

    // Reset clean modal tabs to default (manual tab)
    const cleanModal = document.getElementById("clean-cache-modal");
    if (cleanModal) {
        cleanModal.querySelectorAll(".media-tab-btn").forEach(b => {
            if (b.getAttribute("data-mediatab") === "clean-manual") {
                b.classList.add("active");
            } else {
                b.classList.remove("active");
            }
        });
        cleanModal.querySelectorAll(".media-tab-panel").forEach(p => {
            if (p.id === "mediatab-clean-manual") {
                p.classList.add("active");
            } else {
                p.classList.remove("active");
            }
        });
        const btnManualCalc = cleanModal.querySelector("#btn-manual-calc");
        const btnManualSubmit = cleanModal.querySelector("#btn-manual-submit");
        const btnAutoTrigger = cleanModal.querySelector("#btn-auto-trigger");
        const btnAutoSave = cleanModal.querySelector("#btn-auto-save");
        if (btnManualCalc) btnManualCalc.style.display = "inline-block";
        if (btnManualSubmit) btnManualSubmit.style.display = "inline-block";
        if (btnAutoTrigger) btnAutoTrigger.style.display = "none";
        if (btnAutoSave) btnAutoSave.style.display = "none";
    }
};

window.toggleAllCleanGenres = function(checked) {
    const unspecified = document.getElementById("clean-genre-unspecified");
    if (unspecified) unspecified.checked = checked;
    
    const checkboxes = document.getElementsByName("clean-genre-checkbox");
    checkboxes.forEach(chk => chk.checked = checked);
};

window.invertCleanGenres = function() {
    const unspecified = document.getElementById("clean-genre-unspecified");
    if (unspecified) unspecified.checked = !unspecified.checked;
    
    const checkboxes = document.getElementsByName("clean-genre-checkbox");
    checkboxes.forEach(chk => chk.checked = !chk.checked);
};

function getSelectedCleanGenres() {
    const selected = [];
    const unspecified = document.getElementById("clean-genre-unspecified");
    if (unspecified && unspecified.checked) {
        selected.push("");
    }
    
    const checkboxes = document.getElementsByName("clean-genre-checkbox");
    checkboxes.forEach(chk => {
        if (chk.checked) {
            selected.push(chk.value);
        }
    });
    return selected;
}

window.calculateCleanSpace = async function() {
    const mediaType = document.getElementById("clean-media-type").value;
    const genres = getSelectedCleanGenres();
    const excludeGenres = document.getElementById("clean-genre-exclude").checked;
    const maxQueryTimesVal = document.getElementById("clean-max-query-times").value.trim();
    const maxQueryTimes = maxQueryTimesVal !== "" ? parseInt(maxQueryTimesVal, 10) : null;

    const infoBox = document.getElementById("clean-cache-preview-info");
    infoBox.innerHTML = '正在计算，请稍候...';
    infoBox.style.borderLeftColor = "var(--primary-color)";

    try {
        const res = await window.apiPost("/media/cache/clean", {
            media_type: mediaType,
            genres: genres,
            exclude_genres: excludeGenres,
            max_query_times: maxQueryTimes,
            dry_run: true
        });
        if (res && res.status === "success") {
            const formattedSize = window.formatBytes(res.size_bytes);
            infoBox.innerHTML = `<strong>预估结果：</strong><br>匹配的缓存文件数: <strong>${res.count}</strong> 个<br>预计可释放空间: <strong>${formattedSize}</strong>`;
            infoBox.style.borderLeftColor = "var(--success-color, #4caf50)";
        } else {
            infoBox.innerHTML = `计算失败: ${res.message || "请求出错"}`;
            infoBox.style.borderLeftColor = "var(--danger-color, #f44336)";
        }
    } catch (e) {
        infoBox.innerHTML = `计算出错: ${e.message}`;
        infoBox.style.borderLeftColor = "var(--danger-color, #f44336)";
    }
};

window.submitCleanCache = async function() {
    const mediaType = document.getElementById("clean-media-type").value;
    const genres = getSelectedCleanGenres();
    const excludeGenres = document.getElementById("clean-genre-exclude").checked;
    const maxQueryTimesVal = document.getElementById("clean-max-query-times").value.trim();
    const maxQueryTimes = maxQueryTimesVal !== "" ? parseInt(maxQueryTimesVal, 10) : null;

    window.showConfirm("确认清理缓存", "确定要清理符合条件的媒体文件缓存吗？此操作将物理删除本地缓存文件（保留转述文字描述），不可逆。", async () => {
        try {
            const res = await window.apiPost("/media/cache/clean", {
                media_type: mediaType,
                genres: genres,
                exclude_genres: excludeGenres,
                max_query_times: maxQueryTimes,
                dry_run: false
            });
            if (res && res.status === "success") {
                const formattedSize = window.formatBytes(res.size_bytes);
                window.showToast(`清理成功！共清理 ${res.count} 个文件，释放空间 ${formattedSize}`);
                window.closeModal("clean-cache-modal");
                window.GiftiaApp.pagination.media.page = 1;
                window.GiftiaApp.loadMedia();
            } else {
                window.showToast(`清理失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.loadAutoCleanConfig = async function() {
    const container = document.getElementById("auto-clean-genre-container");
    container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">加载中...</div>';
    
    try {
        // Fetch config and distinct genres
        const configRes = await window.apiGet("/media/cache/auto_clean/config");
        const genresRes = await window.apiGet("/media/genres");
        
        if (configRes && configRes.status === "success" && genresRes && genresRes.status === "success") {
            const config = configRes.config || { enabled: false, keep_genres: ["表情包", "sticker"] };
            const enabledCheckbox = document.getElementById("auto-clean-enabled");
            if (enabledCheckbox) {
                enabledCheckbox.checked = config.enabled;
            }
            
            container.innerHTML = "";
            
            // Add unspecified genre checkbox
            const unspecifiedDiv = document.createElement("div");
            unspecifiedDiv.style.display = "flex";
            unspecifiedDiv.style.alignItems = "center";
            unspecifiedDiv.style.gap = "6px";
            unspecifiedDiv.style.margin = "4px 0";
            const isUnspecifiedChecked = config.keep_genres.includes("");
            unspecifiedDiv.innerHTML = `
                <input type="checkbox" id="auto-clean-genre-unspecified" value="" style="width: auto; margin: 0; cursor: pointer;" ${isUnspecifiedChecked ? "checked" : ""}>
                <label for="auto-clean-genre-unspecified" style="margin: 0; cursor: pointer; font-weight: normal; color: var(--font-color);">[未指定风格]</label>
            `;
            container.appendChild(unspecifiedDiv);
            
            // Add other genres
            genresRes.genres.forEach((genre, idx) => {
                const genreDiv = document.createElement("div");
                genreDiv.style.display = "flex";
                genreDiv.style.alignItems = "center";
                genreDiv.style.gap = "6px";
                genreDiv.style.margin = "4px 0";
                const isChecked = config.keep_genres.includes(genre);
                genreDiv.innerHTML = `
                    <input type="checkbox" name="auto-clean-genre-checkbox" id="auto-clean-genre-chk-${idx}" value="${window.escapeHtml(genre)}" style="width: auto; margin: 0; cursor: pointer;" ${isChecked ? "checked" : ""}>
                    <label for="auto-clean-genre-chk-${idx}" style="margin: 0; cursor: pointer; font-weight: normal; color: var(--font-color);">${window.escapeHtml(genre)}</label>
                `;
                container.appendChild(genreDiv);
            });
        } else {
            container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">加载配置失败。</div>';
        }
    } catch (e) {
        console.error("Failed to load auto-clean config:", e);
        container.innerHTML = '<div style="font-size: 12px; color: var(--font-secondary);">加载配置出错。</div>';
    }
};

window.saveAutoCleanConfig = async function() {
    const enabledCheckbox = document.getElementById("auto-clean-enabled");
    const enabled = enabledCheckbox ? enabledCheckbox.checked : false;
    
    const keep_genres = [];
    const unspecifiedChk = document.getElementById("auto-clean-genre-unspecified");
    if (unspecifiedChk && unspecifiedChk.checked) {
        keep_genres.push("");
    }
    
    document.querySelectorAll('input[name="auto-clean-genre-checkbox"]').forEach(chk => {
        if (chk.checked) {
            keep_genres.push(chk.value);
        }
    });
    
    try {
        const res = await window.apiPost("/media/cache/auto_clean/config", {
            enabled: enabled,
            keep_genres: keep_genres
        });
        
        if (res && res.status === "success") {
            window.showToast("自动清理配置保存成功！");
        } else {
            window.showToast(`保存失败: ${res.message || "未知错误"}`);
        }
    } catch (e) {
        console.error("Failed to save auto-clean config:", e);
        window.showToast("保存配置出错");
    }
};

window.triggerAutoCleanImmediately = async function() {
    window.showConfirm("确认执行自动清理", "确认要立即运行一次自动清理吗？这将按照当前设定的规则，物理删除过期超出会话窗口且不属于保留范围的媒体缓存文件，不可逆。", async () => {
        try {
            const res = await window.apiPost("/media/cache/auto_clean/trigger", {});
            if (res && res.status === "success") {
                const formattedSize = window.formatBytes(res.size_bytes);
                window.showToast(`清理成功！共释放空间 ${formattedSize}，物理删除 ${res.count} 个文件`);
                window.closeModal("clean-cache-modal");
                window.GiftiaApp.pagination.media.page = 1;
                window.GiftiaApp.loadMedia();
            } else {
                window.showToast(`执行清理失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            console.error("Failed to trigger auto-clean:", e);
            window.showToast("触发自动清理出错");
        }
    });
};
