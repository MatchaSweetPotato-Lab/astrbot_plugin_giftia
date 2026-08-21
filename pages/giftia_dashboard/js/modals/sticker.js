// Giftia Dashboard Modals - Sticker Management

import { state } from '../modules/state.js';
import { loadStickers, loadStickerFilterOptions, updateStickerSelectionUI } from '../modules/render/stickers.js';

// 每批上传张数：base64 走 bridge 会膨胀约 33%，分批避免单请求过大
const UPLOAD_CHUNK_SIZE = 5;
// 与后端 MAX_STICKER_BYTES 保持一致
const MAX_STICKER_BYTES = 10 * 1024 * 1024;

let editTagsSelect = null;
let editBotsSelect = null;
let uploadTagsSelect = null;
let uploadBotsSelect = null;
let batchTagsSelect = null;
let pendingUploadFiles = [];

/** 刷新画廊与筛选项（写操作成功后调用） */
async function refreshStickerView() {
    await loadStickerFilterOptions();
    await loadStickers();
}

function destroySelect(instance) {
    if (instance && typeof instance.destroy === "function") {
        instance.destroy();
    }
}

/** 把字符串数组包装成 TagSelectComponent 需要的 {id, name} 选项 */
function toOptions(values) {
    return (values || []).map(v => ({ id: v, name: v }));
}

// ─── 1. 编辑表情包 ────────────────────────────────────────────────────────

window.openEditStickerModal = function(sticker) {
    if (!sticker) return;

    document.getElementById("edit-sticker-id").value = sticker.sticker_id;
    document.getElementById("edit-sticker-name").value = sticker.name || "";
    document.getElementById("edit-sticker-category").value = sticker.category || "";
    document.getElementById("edit-sticker-description").value = sticker.description || "";

    const titleEl = document.getElementById("edit-sticker-title");
    if (titleEl) titleEl.textContent = `编辑表情包 (${sticker.sticker_id})`;

    // 分类下拉建议
    const datalist = document.getElementById("sticker-category-suggestions");
    if (datalist) {
        datalist.innerHTML = (state.stickerOptions.categories || [])
            .map(c => `<option value="${window.escapeHtml(c)}"></option>`).join("");
    }

    destroySelect(editTagsSelect);
    editTagsSelect = new window.TagSelectComponent("edit-sticker-tags", {
        placeholder: "检索已有标签，或输入新标签后回车...",
        availableOptions: toOptions(state.stickerOptions.tags),
        selectedValues: [...(sticker.tags || [])],
    });

    destroySelect(editBotsSelect);
    editBotsSelect = new window.TagSelectComponent("edit-sticker-bots", {
        placeholder: "选择哪些机器人可以发送这张表情包...",
        availableOptions: toOptions(state.stickerOptions.bots),
        selectedValues: [...(sticker.bot_names || [])],
    });

    // 预览图
    const preview = document.getElementById("edit-sticker-preview");
    if (preview) {
        preview.innerHTML = `<img id="edit-sticker-preview-img" alt="表情包预览">`;
        const gridImg = document.getElementById(`sticker-preview-${sticker.sticker_id}`);
        if (gridImg && gridImg.src && gridImg.src.startsWith("data:")) {
            // 复用画廊里已加载的图，省一次请求
            document.getElementById("edit-sticker-preview-img").src = gridImg.src;
        }
        // 弹窗里始终换成原图（GIF 可看动画）
        window.apiGet(`/stickers/file/b64/${sticker.sticker_id}`).then(res => {
            const img = document.getElementById("edit-sticker-preview-img");
            if (img && res && res.status === "success" && res.base64) {
                img.src = `data:${res.content_type || "image/png"};base64,${res.base64}`;
            } else if (img) {
                preview.innerHTML = `<div class="sticker-missing"><div class="sticker-missing-icon">🖼️</div><div class="sticker-missing-hint">图片文件缺失</div></div>`;
            }
        }).catch(() => {});
    }

    const aiBtn = document.getElementById("btn-reanalyze-sticker");
    if (aiBtn) {
        aiBtn.disabled = !state.stickerOptions.ai_available;
        aiBtn.title = state.stickerOptions.ai_available
            ? "调用视觉模型重新识别名称/分类/标签（会消耗 Token）"
            : "未配置图片转述模型 (image_caption_provider_ids)，无法使用";
    }

    window.openModal("edit-sticker-modal");
};

window.submitEditSticker = async function() {
    const stickerId = document.getElementById("edit-sticker-id").value;
    const name = document.getElementById("edit-sticker-name").value.trim();
    const category = document.getElementById("edit-sticker-category").value.trim();
    const description = document.getElementById("edit-sticker-description").value.trim();

    if (!name) {
        window.showToast("表情包名称不能为空");
        return;
    }

    const payload = {
        sticker_id: stickerId,
        name,
        category,
        description,
        tags: editTagsSelect ? editTagsSelect.getValues() : [],
        bot_names: editBotsSelect ? editBotsSelect.getValues() : [],
    };

    try {
        const res = await window.apiPost("/stickers/update", payload);
        if (res.status === "success") {
            window.showToast(res.message || "保存成功");
            window.closeModal("edit-sticker-modal");
            await refreshStickerView();
        } else {
            window.showToast(res.message || "保存失败");
        }
    } catch (e) {
        window.showToast(`保存失败: ${e.message}`);
    }
};

window.reanalyzeSticker = async function() {
    const stickerId = document.getElementById("edit-sticker-id").value;
    if (!stickerId) return;

    const btn = document.getElementById("btn-reanalyze-sticker");
    const originalText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "AI 分析中...";
    }

    try {
        const res = await window.apiPost("/stickers/analyze", { sticker_id: stickerId });
        if (res.status === "success") {
            window.showToast(res.message || "AI 分析完成");
            if (res.changed && res.data) {
                document.getElementById("edit-sticker-name").value = res.data.name || "";
                document.getElementById("edit-sticker-category").value = res.data.category || "";
                document.getElementById("edit-sticker-description").value = res.data.description || "";
                destroySelect(editTagsSelect);
                editTagsSelect = new window.TagSelectComponent("edit-sticker-tags", {
                    placeholder: "检索已有标签，或输入新标签后回车...",
                    availableOptions: toOptions(state.stickerOptions.tags),
                    selectedValues: [...(res.data.tags || [])],
                });
            }
        } else {
            window.showToast(res.message || "AI 分析失败");
        }
    } catch (e) {
        window.showToast(`AI 分析失败: ${e.message}`);
    } finally {
        if (btn) {
            btn.disabled = !state.stickerOptions.ai_available;
            btn.textContent = originalText || "重新 AI 分析";
        }
    }
};

// ─── 2. 删除 ──────────────────────────────────────────────────────────────

window.deleteSticker = function(stickerId, name) {
    window.showConfirm(
        "确认删除表情包",
        `确定要删除「${name || stickerId}」吗？数据库记录、机器人归属和本地图片文件都会被清理，此操作无法撤销。`,
        async () => {
            try {
                const res = await window.apiPost("/stickers/delete", { sticker_id: stickerId });
                if (res.status === "success") {
                    window.showToast(res.message || "删除成功");
                    state.selectedStickers.delete(stickerId);
                    await refreshStickerView();
                } else {
                    window.showToast(res.message || "删除失败");
                }
            } catch (e) {
                window.showToast(`删除失败: ${e.message}`);
            }
        }
    );
};

// ─── 3. 上传 ──────────────────────────────────────────────────────────────

window.openUploadStickerModal = function() {
    pendingUploadFiles = [];

    document.getElementById("upload-sticker-name").value = "";
    document.getElementById("upload-sticker-category").value = "";
    document.getElementById("upload-sticker-description").value = "";
    const fileInput = document.getElementById("upload-sticker-files");
    if (fileInput) fileInput.value = "";

    const datalist = document.getElementById("sticker-category-suggestions");
    if (datalist) {
        datalist.innerHTML = (state.stickerOptions.categories || [])
            .map(c => `<option value="${window.escapeHtml(c)}"></option>`).join("");
    }

    destroySelect(uploadTagsSelect);
    uploadTagsSelect = new window.TagSelectComponent("upload-sticker-tags", {
        placeholder: "检索已有标签，或输入新标签后回车...",
        availableOptions: toOptions(state.stickerOptions.tags),
        selectedValues: [],
    });

    destroySelect(uploadBotsSelect);
    uploadBotsSelect = new window.TagSelectComponent("upload-sticker-bots", {
        placeholder: "选择上传后直接归属给哪些机器人...",
        availableOptions: toOptions(state.stickerOptions.bots),
        selectedValues: [],
    });

    const aiToggle = document.getElementById("upload-sticker-ai");
    const aiHint = document.getElementById("upload-sticker-ai-hint");
    if (aiToggle) {
        aiToggle.checked = false;
        aiToggle.disabled = !state.stickerOptions.ai_available;
    }
    if (aiHint) {
        aiHint.textContent = state.stickerOptions.ai_available
            ? "勾选后调用视觉模型自动识别名称/分类/标签，每张图都会消耗 Token。"
            : "未配置图片转述模型 (image_caption_provider_ids)，无法使用 AI 分析。";
    }

    renderUploadPreview();
    renderUploadProgress([]);
    window.openModal("upload-sticker-modal");
};

/** 收集用户选择/拖入的文件，做前端预校验 */
function acceptUploadFiles(fileList) {
    const rejected = [];
    Array.from(fileList || []).forEach(file => {
        if (!file.type.startsWith("image/")) {
            rejected.push(`${file.name}（不是图片）`);
            return;
        }
        if (file.size > MAX_STICKER_BYTES) {
            rejected.push(`${file.name}（超过 ${Math.floor(MAX_STICKER_BYTES / 1024 / 1024)}MB）`);
            return;
        }
        if (pendingUploadFiles.some(f => f.name === file.name && f.size === file.size)) {
            return;
        }
        pendingUploadFiles.push(file);
    });

    if (rejected.length) {
        window.showToast(`已跳过 ${rejected.length} 个文件：${rejected.slice(0, 3).join("、")}`);
    }
    renderUploadPreview();
}

function renderUploadPreview() {
    const box = document.getElementById("upload-sticker-preview");
    const countEl = document.getElementById("upload-sticker-count");
    if (countEl) countEl.textContent = String(pendingUploadFiles.length);
    if (!box) return;

    if (!pendingUploadFiles.length) {
        box.innerHTML = `<div class="upload-empty-hint">还没有选择图片</div>`;
        return;
    }

    box.innerHTML = pendingUploadFiles.map((file, idx) => `
        <div class="upload-preview-item" data-upload-index="${idx}">
            <img alt="${window.escapeHtml(file.name)}">
            <button type="button" class="upload-preview-remove" data-upload-remove="${idx}" title="移除">&times;</button>
            <div class="upload-preview-name" title="${window.escapeHtml(file.name)}">${window.escapeHtml(file.name)}</div>
        </div>
    `).join("");

    // 本地预览，不上传也能看到
    pendingUploadFiles.forEach((file, idx) => {
        const item = box.querySelector(`[data-upload-index="${idx}"] img`);
        if (!item) return;
        const reader = new FileReader();
        reader.onload = () => { item.src = reader.result; };
        reader.readAsDataURL(file);
    });
}

function renderUploadProgress(results) {
    const box = document.getElementById("upload-sticker-results");
    if (!box) return;

    if (!results || !results.length) {
        box.innerHTML = "";
        box.style.display = "none";
        return;
    }

    box.style.display = "block";
    box.innerHTML = results.map(r => {
        const cls = r.status === "added" ? "badge-success"
            : r.status === "exists" ? "badge-secondary" : "badge-danger";
        const label = r.status === "added" ? "已添加"
            : r.status === "exists" ? "已存在" : "失败";
        return `
            <div class="upload-result-row">
                <span class="badge ${cls}">${label}</span>
                <span class="upload-result-file" title="${window.escapeHtml(r.filename || "")}">${window.escapeHtml(r.filename || "")}</span>
                <span class="upload-result-msg">${window.escapeHtml(r.message || "")}</span>
            </div>
        `;
    }).join("");
}

/** 读文件为纯 base64（去掉 data: 前缀） */
function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const result = String(reader.result || "");
            const comma = result.indexOf(",");
            resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(new Error(`读取文件失败: ${file.name}`));
        reader.readAsDataURL(file);
    });
}

window.submitUploadStickers = async function() {
    if (!pendingUploadFiles.length) {
        window.showToast("请先选择要上传的图片");
        return;
    }

    const name = document.getElementById("upload-sticker-name").value.trim();
    const category = document.getElementById("upload-sticker-category").value.trim();
    const description = document.getElementById("upload-sticker-description").value.trim();
    const useAi = !!document.getElementById("upload-sticker-ai")?.checked;
    const tags = uploadTagsSelect ? uploadTagsSelect.getValues() : [];
    const bindBots = uploadBotsSelect ? uploadBotsSelect.getValues() : [];

    if (!useAi && !name && pendingUploadFiles.length === 1) {
        window.showToast("未填名称时会用文件名作为表情包名称");
    }

    const btn = document.getElementById("btn-submit-upload");
    const originalText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
    }

    const allResults = [];
    let added = 0, skipped = 0, failed = 0;

    try {
        // 分批上传：base64 膨胀约 33%，单请求塞太多容易超限
        for (let i = 0; i < pendingUploadFiles.length; i += UPLOAD_CHUNK_SIZE) {
            const chunk = pendingUploadFiles.slice(i, i + UPLOAD_CHUNK_SIZE);
            const done = i;
            if (btn) {
                btn.textContent = `上传中 ${done}/${pendingUploadFiles.length}...`;
            }

            let files;
            try {
                files = await Promise.all(chunk.map(async file => ({
                    filename: file.name,
                    content: await readFileAsBase64(file),
                })));
            } catch (readErr) {
                chunk.forEach(file => {
                    failed += 1;
                    allResults.push({ filename: file.name, status: "failed", message: readErr.message });
                });
                renderUploadProgress(allResults);
                continue;
            }

            try {
                const res = await window.apiPost("/stickers/upload", {
                    files,
                    name,
                    category,
                    description,
                    tags,
                    ai_analysis: useAi,
                    bind_bots: bindBots,
                });

                if (res.status === "success" && res.data) {
                    added += res.data.added || 0;
                    skipped += res.data.skipped || 0;
                    failed += res.data.failed || 0;
                    allResults.push(...(res.data.results || []));
                } else {
                    chunk.forEach(file => {
                        failed += 1;
                        allResults.push({
                            filename: file.name,
                            status: "failed",
                            message: res.message || "上传失败",
                        });
                    });
                }
            } catch (e) {
                chunk.forEach(file => {
                    failed += 1;
                    allResults.push({ filename: file.name, status: "failed", message: e.message });
                });
            }

            renderUploadProgress(allResults);
        }

        let summary = `新增 ${added} 张`;
        if (skipped) summary += `，跳过重复 ${skipped} 张`;
        if (failed) summary += `，失败 ${failed} 张`;
        window.showToast(summary);

        // 成功上传的从待上传列表里移除，失败的留着让用户重试
        const failedNames = new Set(
            allResults.filter(r => r.status === "failed").map(r => r.filename)
        );
        pendingUploadFiles = pendingUploadFiles.filter(f => failedNames.has(f.name));
        renderUploadPreview();

        await refreshStickerView();

        if (!failed) {
            window.closeModal("upload-sticker-modal");
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText || "开始上传";
        }
    }
};

// ─── 4. 批量操作 ──────────────────────────────────────────────────────────

function selectedIds() {
    return Array.from(state.selectedStickers);
}

async function submitBatch(payload, successCallback) {
    try {
        const res = await window.apiPost("/stickers/batch", payload);
        if (res.status === "success") {
            window.showToast(res.message || "操作成功");
            if (typeof successCallback === "function") successCallback();
            await refreshStickerView();
        } else {
            window.showToast(res.message || "操作失败");
        }
    } catch (e) {
        window.showToast(`操作失败: ${e.message}`);
    }
}

window.openBatchStickerModal = function(mode) {
    const ids = selectedIds();
    if (!ids.length) {
        window.showToast("请先选择表情包");
        return;
    }

    document.getElementById("batch-sticker-mode").value = mode;
    document.getElementById("batch-sticker-count").textContent = String(ids.length);

    const titles = {
        set_category: "批量修改分类",
        add_tags: "批量追加标签",
        remove_tags: "批量移除标签",
        link_bot: "批量绑定机器人",
        unlink_bot: "批量解绑机器人",
    };
    document.getElementById("batch-sticker-title").textContent = titles[mode] || "批量操作";

    const categoryRow = document.getElementById("batch-sticker-category-row");
    const tagsRow = document.getElementById("batch-sticker-tags-row");
    const botRow = document.getElementById("batch-sticker-bot-row");

    categoryRow.style.display = mode === "set_category" ? "block" : "none";
    tagsRow.style.display = (mode === "add_tags" || mode === "remove_tags") ? "block" : "none";
    botRow.style.display = (mode === "link_bot" || mode === "unlink_bot") ? "block" : "none";

    if (mode === "set_category") {
        document.getElementById("batch-sticker-category").value = "";
        const datalist = document.getElementById("sticker-category-suggestions");
        if (datalist) {
            datalist.innerHTML = (state.stickerOptions.categories || [])
                .map(c => `<option value="${window.escapeHtml(c)}"></option>`).join("");
        }
    }

    if (mode === "add_tags" || mode === "remove_tags") {
        destroySelect(batchTagsSelect);
        batchTagsSelect = new window.TagSelectComponent("batch-sticker-tags", {
            placeholder: mode === "add_tags"
                ? "输入要追加的标签，回车确认..."
                : "选择要移除的标签...",
            availableOptions: toOptions(state.stickerOptions.tags),
            selectedValues: [],
        });
    }

    if (mode === "link_bot" || mode === "unlink_bot") {
        const select = document.getElementById("batch-sticker-bot");
        if (select) {
            select.innerHTML = "";
            (state.stickerOptions.bots || []).forEach(b => select.append(new Option(b, b)));
        }
    }

    window.openModal("batch-sticker-modal");
};

window.submitBatchSticker = async function() {
    const mode = document.getElementById("batch-sticker-mode").value;
    const ids = selectedIds();
    if (!ids.length) {
        window.showToast("请先选择表情包");
        return;
    }

    const payload = { action: mode, sticker_ids: ids };

    if (mode === "set_category") {
        const category = document.getElementById("batch-sticker-category").value.trim();
        if (!category) {
            window.showToast("请填写分类名称");
            return;
        }
        payload.category = category;
    } else if (mode === "add_tags" || mode === "remove_tags") {
        const tags = batchTagsSelect ? batchTagsSelect.getValues() : [];
        if (!tags.length) {
            window.showToast("请至少选择一个标签");
            return;
        }
        payload.tags = tags;
    } else if (mode === "link_bot" || mode === "unlink_bot") {
        const botName = document.getElementById("batch-sticker-bot").value;
        if (!botName) {
            window.showToast("请选择机器人");
            return;
        }
        payload.bot_name = botName;
    }

    await submitBatch(payload, () => {
        window.closeModal("batch-sticker-modal");
        state.selectedStickers.clear();
        updateStickerSelectionUI();
    });
};

window.batchDeleteStickers = function() {
    const ids = selectedIds();
    if (!ids.length) {
        window.showToast("请先选择表情包");
        return;
    }

    window.showConfirm(
        "确认批量删除表情包",
        `确定要删除选中的 ${ids.length} 张表情包吗？数据库记录、机器人归属和本地图片文件都会被清理，此操作无法撤销。`,
        async () => {
            await submitBatch({ action: "delete", sticker_ids: ids }, () => {
                state.selectedStickers.clear();
                updateStickerSelectionUI();
            });
        }
    );
};

// ─── 5. 分类与标签管理 ────────────────────────────────────────────────────

window.openStickerTaxonomyModal = async function() {
    window.openModal("sticker-taxonomy-modal");
    await Promise.all([loadTaxonomyCategories(), loadTaxonomyTags()]);
};

async function loadTaxonomyCategories() {
    const box = document.getElementById("taxonomy-category-list");
    if (!box) return;
    box.innerHTML = `<div class="loading-row"><span class="loader"></span> 加载分类...</div>`;

    try {
        const res = await window.apiGet("/stickers/categories");
        const categories = res?.data?.categories || [];
        if (!categories.length) {
            box.innerHTML = `<div class="no-data-row">暂无分类</div>`;
            return;
        }
        box.innerHTML = categories.map(c => {
            const label = c.category || "未分类";
            return `
                <div class="taxonomy-row">
                    <span class="taxonomy-name" title="${window.escapeHtml(label)}">${window.escapeHtml(label)}</span>
                    <span class="taxonomy-count">${c.count} 张</span>
                    <button class="btn btn-secondary btn-small"
                            data-taxonomy-rename-category="${window.escapeHtml(c.category || "")}">重命名</button>
                </div>
            `;
        }).join("");
    } catch (e) {
        box.innerHTML = `<div class="no-data-row">加载失败: ${window.escapeHtml(e.message)}</div>`;
    }
}

async function loadTaxonomyTags() {
    const box = document.getElementById("taxonomy-tag-list");
    if (!box) return;
    box.innerHTML = `<div class="loading-row"><span class="loader"></span> 加载标签...</div>`;

    try {
        const res = await window.apiGet("/stickers/tags");
        const tags = res?.data?.tags || [];
        if (!tags.length) {
            box.innerHTML = `<div class="no-data-row">暂无标签</div>`;
            return;
        }
        box.innerHTML = tags.map(t => `
            <div class="taxonomy-row">
                <span class="taxonomy-name" title="${window.escapeHtml(t.tag)}">${window.escapeHtml(t.tag)}</span>
                <span class="taxonomy-count">${t.count} 张</span>
                <button class="btn btn-secondary btn-small"
                        data-taxonomy-rename-tag="${window.escapeHtml(t.tag)}">重命名</button>
                <button class="btn btn-danger btn-small"
                        data-taxonomy-delete-tag="${window.escapeHtml(t.tag)}">删除</button>
            </div>
        `).join("");
    } catch (e) {
        box.innerHTML = `<div class="no-data-row">加载失败: ${window.escapeHtml(e.message)}</div>`;
    }
}

async function refreshTaxonomy() {
    await Promise.all([loadTaxonomyCategories(), loadTaxonomyTags()]);
    await refreshStickerView();
}

function promptRename(kind, oldValue) {
    const label = kind === "category" ? "分类" : "标签";
    const display = oldValue || "未分类";
    const next = window.prompt(
        `把${label}「${display}」重命名为：（若填入已存在的名字，两者会合并）`,
        oldValue
    );
    if (next === null) return null;
    const trimmed = next.trim();
    if (!trimmed) {
        window.showToast(`${label}名称不能为空`);
        return null;
    }
    if (trimmed === oldValue) return null;
    return trimmed;
}

async function renameCategory(oldName) {
    const newName = promptRename("category", oldName);
    if (!newName) return;

    const existing = (state.stickerOptions.categories || []).includes(newName);
    const doIt = async () => {
        try {
            const res = await window.apiPost("/stickers/categories/rename", {
                old_name: oldName,
                new_name: newName,
            });
            window.showToast(res.message || (res.status === "success" ? "重命名成功" : "重命名失败"));
            if (res.status === "success") await refreshTaxonomy();
        } catch (e) {
            window.showToast(`重命名失败: ${e.message}`);
        }
    };

    if (existing) {
        window.showConfirm(
            "合并分类",
            `分类「${newName}」已存在，继续将把「${oldName || "未分类"}」下的表情包全部并入其中。要继续吗？`,
            doIt
        );
    } else {
        await doIt();
    }
}

async function renameTag(oldTag) {
    const newTag = promptRename("tag", oldTag);
    if (!newTag) return;

    const existing = (state.stickerOptions.tags || []).includes(newTag);
    const doIt = async () => {
        try {
            const res = await window.apiPost("/stickers/tags/rename", {
                old_tag: oldTag,
                new_tag: newTag,
            });
            window.showToast(res.message || (res.status === "success" ? "重命名成功" : "重命名失败"));
            if (res.status === "success") await refreshTaxonomy();
        } catch (e) {
            window.showToast(`重命名失败: ${e.message}`);
        }
    };

    if (existing) {
        window.showConfirm(
            "合并标签",
            `标签「${newTag}」已存在，继续将把「${oldTag}」合并进去（同一张表情包上会自动去重）。要继续吗？`,
            doIt
        );
    } else {
        await doIt();
    }
}

function deleteTag(tag) {
    window.showConfirm(
        "删除标签",
        `确定要从所有表情包上移除标签「${tag}」吗？表情包本身不会被删除。`,
        async () => {
            try {
                const res = await window.apiPost("/stickers/tags/delete", { tag });
                window.showToast(res.message || (res.status === "success" ? "删除成功" : "删除失败"));
                if (res.status === "success") await refreshTaxonomy();
            } catch (e) {
                window.showToast(`删除失败: ${e.message}`);
            }
        }
    );
}

// ─── 事件绑定 ─────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    // 分类/标签管理弹窗内的按钮走事件委托（列表是动态渲染的）
    const taxonomyModal = document.getElementById("sticker-taxonomy-modal");
    if (taxonomyModal) {
        taxonomyModal.addEventListener("click", (e) => {
            const renameCat = e.target.closest("[data-taxonomy-rename-category]");
            if (renameCat) {
                renameCategory(renameCat.getAttribute("data-taxonomy-rename-category"));
                return;
            }
            const renameT = e.target.closest("[data-taxonomy-rename-tag]");
            if (renameT) {
                renameTag(renameT.getAttribute("data-taxonomy-rename-tag"));
                return;
            }
            const deleteT = e.target.closest("[data-taxonomy-delete-tag]");
            if (deleteT) {
                deleteTag(deleteT.getAttribute("data-taxonomy-delete-tag"));
            }
        });
    }

    // 上传弹窗：文件选择、拖拽、移除
    const fileInput = document.getElementById("upload-sticker-files");
    if (fileInput) {
        fileInput.addEventListener("change", (e) => {
            acceptUploadFiles(e.target.files);
            e.target.value = "";
        });
    }

    const dropZone = document.getElementById("upload-sticker-dropzone");
    if (dropZone) {
        ["dragenter", "dragover"].forEach(evt => {
            dropZone.addEventListener(evt, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.add("dragover");
            });
        });
        ["dragleave", "drop"].forEach(evt => {
            dropZone.addEventListener(evt, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.remove("dragover");
            });
        });
        dropZone.addEventListener("drop", (e) => {
            acceptUploadFiles(e.dataTransfer?.files);
        });
        dropZone.addEventListener("click", () => {
            document.getElementById("upload-sticker-files")?.click();
        });
    }

    const previewBox = document.getElementById("upload-sticker-preview");
    if (previewBox) {
        previewBox.addEventListener("click", (e) => {
            const removeBtn = e.target.closest("[data-upload-remove]");
            if (removeBtn) {
                const idx = parseInt(removeBtn.getAttribute("data-upload-remove"), 10);
                if (!Number.isNaN(idx)) {
                    pendingUploadFiles.splice(idx, 1);
                    renderUploadPreview();
                }
            }
        });
    }
});
