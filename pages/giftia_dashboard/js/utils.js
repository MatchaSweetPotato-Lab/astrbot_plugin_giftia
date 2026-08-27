// Giftia Dashboard Utilities

window.formatDate = function(isoString) {
    if (!isoString) return "-";
    try {
        const date = new Date(isoString);
        if (isNaN(date.getTime())) {
            return isoString;
        }
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, "0");
        const d = String(date.getDate()).padStart(2, "0");
        const h = String(date.getHours()).padStart(2, "0");
        const min = String(date.getMinutes()).padStart(2, "0");
        const s = String(date.getSeconds()).padStart(2, "0");
        return `${y}-${m}-${d} ${h}:${min}:${s}`;
    } catch {
        return isoString;
    }
};

window.escapeHtml = function(text) {
    if (!text) return "";
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
};

window.showToast = function(message) {
    const toast = document.getElementById("toast-message");
    if (toast) {
        toast.textContent = message;
        toast.classList.add("show");
        setTimeout(() => {
            toast.classList.remove("show");
        }, 3000);
    }
};

window.copyToClipboard = function(text, label) {
    if (!text) return;
    
    const successMessage = `${label || "内容"}已复制到剪贴板`;
    
    const fallbackCopy = (val) => {
        try {
            const textArea = document.createElement("textarea");
            textArea.value = val;
            textArea.style.top = "0";
            textArea.style.left = "0";
            textArea.style.position = "fixed";
            textArea.style.opacity = "0";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            const successful = document.execCommand("copy");
            document.body.removeChild(textArea);
            if (successful) {
                window.showToast(successMessage);
            } else {
                window.showToast("复制失败，请手动复制");
            }
        } catch (err) {
            console.error("fallbackCopy 复制失败:", err);
            window.showToast("复制失败，请手动复制");
        }
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            window.showToast(successMessage);
        }).catch(err => {
            console.error("navigator.clipboard 复制失败:", err);
            fallbackCopy(text);
        });
    } else {
        fallbackCopy(text);
    }
};

window.renderPagination = function(containerId, state, onPageChange) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const totalPages = Math.ceil(state.total / state.limit);
    if (totalPages <= 1) {
        container.innerHTML = `<span>共 ${state.total} 条记录</span>`;
        return;
    }

    const startItem = (state.page - 1) * state.limit + 1;
    const endItem = Math.min(state.page * state.limit, state.total);

    let buttons = "";
    buttons += `<button class="pagination-btn" ${state.page === 1 ? "disabled" : ""} data-page="${state.page - 1}">上一页</button>`;
    
    const startPage = Math.max(1, state.page - 2);
    const endPage = Math.min(totalPages, state.page + 2);
    
    for (let i = startPage; i <= endPage; i++) {
        buttons += `<button class="pagination-btn ${state.page === i ? "active" : ""}" data-page="${i}">${i}</button>`;
    }
    
    buttons += `<button class="pagination-btn" ${state.page === totalPages ? "disabled" : ""} data-page="${state.page + 1}">下一页</button>`;

    container.innerHTML = `
        <span>显示 ${startItem}-${endItem} 条，共 ${state.total} 条</span>
        <div class="pagination-buttons">
            ${buttons}
        </div>
    `;

    const btns = container.querySelectorAll(".pagination-btn");
    btns.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetPage = parseInt(btn.getAttribute("data-page"));
            if (targetPage && targetPage !== state.page) {
                onPageChange(targetPage);
            }
        });
    });
};

window.formatBytes = function(bytes) {
    if (bytes === 0) return "0 字节";
    const k = 1024;
    const sizes = ["字节", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
};

// Confirm Modal logic
let confirmCallback = null;

window.showConfirm = function(title, message, callback) {
    const titleEl = document.getElementById("confirm-title");
    const msgEl = document.getElementById("confirm-message");
    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    confirmCallback = callback;
    window.openModal("confirm-modal");
};

window.closeConfirmModal = function() {
    window.closeModal("confirm-modal");
    confirmCallback = null;
};

document.addEventListener("DOMContentLoaded", () => {
    const confirmBtnOk = document.getElementById("confirm-btn-ok");
    if (confirmBtnOk) {
        confirmBtnOk.addEventListener("click", () => {
            if (confirmCallback) {
                confirmCallback();
            }
            window.closeConfirmModal();
        });
    }
});

window.downloadMedia = async function(hash, mimeType) {
    try {
        window.showToast("正在准备下载...");
        const res = await window.apiGet(`/media/file/b64/${hash}`);
        if (res && res.status === "success" && res.base64) {
            const actualMime = res.content_type || mimeType || "audio/mpeg";
            const byteCharacters = atob(res.base64);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], { type: actualMime });
            const url = URL.createObjectURL(blob);
            
            const a = document.createElement("a");
            a.href = url;
            const ext = actualMime.replace("audio/", "").replace("voice/", "") || "amr";
            a.download = `${hash}.${ext.toLowerCase()}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } else {
            window.showToast("获取音频文件失败");
        }
    } catch (e) {
        console.error("Failed to download media:", e);
        window.showToast("下载失败: " + e.message);
    }
};

window.renderProfileCard = function(rawProfile, structured, isGroup) {
    // If we have structured details, or we can extract them from rawProfile
    let fields = {
        "你的称呼": "",
        "其他外号": "",
        "性格风格": "",
        "兴趣话题": "",
        "互动态度": "",
        "关键约定": "",
        "其他补充": "",
        "头像描述": ""
    };
    
    // Fill from structured if present
    if (structured) {
        fields["你的称呼"] = structured.call_name || "";
        fields["其他外号"] = structured.aliases || "";
        fields["性格风格"] = structured.personality || "";
        fields["兴趣话题"] = structured.interests || "";
        fields["互动态度"] = structured.attitude || "";
        fields["关键约定"] = structured.agreements || "";
        fields["其他补充"] = structured.extra || "";
        fields["头像描述"] = structured.avatar_description || "";
    }
    
    // If some fields are empty, let's try to extract them from rawProfile
    if (rawProfile) {
        const lines = rawProfile.split('\n');
        for (const line of lines) {
            const match = line.match(/^([^：:]+)[：:](.*)$/);
            if (match) {
                const key = match[1].trim();
                const val = match[2].trim();
                if (fields[key] !== undefined && !fields[key]) {
                    fields[key] = val;
                } else if (key === "其他外号" && !fields["其他外号"]) {
                    fields["其他外号"] = val;
                }
            }
        }
    }
    
    // Now let's render it
    let html = `<div class="profile-card-layout">`;
    
    // 1. Render tags / badges: 你的称呼, 其他外号
    const nickname = fields["你的称呼"] || (structured && structured.call_name);
    const aliases = fields["其他外号"] || (structured && structured.aliases);
    
    let tagsHtml = "";
    if (nickname) {
        tagsHtml += `<span class="profile-tag tag-nickname"><i class="tag-icon">👤</i>${window.escapeHtml(nickname)}</span>`;
    }
    if (aliases) {
        const aliasList = String(aliases).split(/[,，\s]+/).filter(x => x.trim());
        aliasList.forEach(alias => {
            tagsHtml += `<span class="profile-tag tag-alias"><i class="tag-icon">🏷️</i>${window.escapeHtml(alias)}</span>`;
        });
    }
    
    if (tagsHtml) {
        html += `<div class="profile-tags-container">${tagsHtml}</div>`;
    }
    
    // 2. Render Tabs Header
    if (!isGroup) {
        html += `
            <div class="card-tabs-header">
                <button class="card-tab-btn active" onclick="window.switchCardTab(this, 'profile')">画像</button>
                <button class="card-tab-btn" onclick="window.switchCardTab(this, 'relation')">关系</button>
            </div>
        `;
    }

    // Extract fields
    const profileFields = [
        { label: "头像描述", val: fields["头像描述"] || "", class: "prop-avatar", color: "var(--accent, #ec4899)" },
        { label: "性格风格", val: fields["性格风格"] || "", class: "prop-personality", color: "var(--primary)" },
        { label: "兴趣话题", val: fields["兴趣话题"] || "", class: "prop-interests", color: "var(--info)" },
        { label: "其他补充", val: fields["其他补充"] || "", class: "prop-extra", color: "var(--font-secondary)" }
    ].filter(f => f.val && String(f.val).trim());

    const relationFields = [
        { label: "互动态度", val: fields["互动态度"] || "", class: "prop-attitude", color: "var(--success)" },
        { label: "关键约定", val: fields["关键约定"] || "", class: "prop-agreements", color: "var(--warning)" }
    ].filter(f => f.val && String(f.val).trim());

    // --- Tab 1: 画像 ---
    html += `<div class="card-tab-content card-tab-profile">`;
    if (profileFields.length > 0) {
        html += `<div class="profile-grid">`;
        profileFields.forEach((field) => {
            html += `
                <div class="profile-grid-item ${field.class}" style="border-left: 3px solid ${field.color}">
                    <div class="profile-grid-label">${field.label}</div>
                    <div class="profile-grid-value"><span class="text-content">${window.escapeHtml(field.val)}</span></div>
                </div>
            `;
        });
        html += `</div>`;
    } else if (rawProfile && !nickname && !aliases && relationFields.length === 0) {
        // Fallback for raw legacy profile
        html += `<div class="profile-raw-content"><span class="text-content" style="white-space: pre-wrap;">${window.escapeHtml(rawProfile)}</span></div>`;
    } else {
        html += `<div class="profile-grid-empty" style="color: var(--font-secondary); padding: 12px; text-align: center; font-size: 13px;">暂无画像数据</div>`;
    }
    html += `</div>`;

    // --- Tab 2: 关系 ---
    if (!isGroup) {
        html += `<div class="card-tab-content card-tab-relation" style="display: none;">`;
        if (relationFields.length > 0) {
            html += `<div class="profile-grid">`;
            relationFields.forEach((field) => {
                html += `
                    <div class="profile-grid-item ${field.class}" style="border-left: 3px solid ${field.color}">
                        <div class="profile-grid-label">${field.label}</div>
                        <div class="profile-grid-value"><span class="text-content">${window.escapeHtml(field.val)}</span></div>
                    </div>
                `;
            });
            html += `</div>`;
        } else {
            html += `<div class="profile-grid-empty" style="color: var(--font-secondary); padding: 12px; text-align: center; font-size: 13px;">暂无关系数据</div>`;
        }
        html += `</div>`;
    }
    
    html += `</div>`;
    return html;
};

window.switchCardTab = function(btn, tabName) {
    const card = btn.closest(".profile-card-layout");
    if (!card) return;
    
    card.querySelectorAll(".card-tab-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    
    const profilePane = card.querySelector(".card-tab-profile");
    const relationPane = card.querySelector(".card-tab-relation");
    
    if (tabName === 'profile') {
        if (profilePane) profilePane.style.display = "block";
        if (relationPane) relationPane.style.display = "none";
    } else {
        if (profilePane) profilePane.style.display = "none";
        if (relationPane) relationPane.style.display = "block";
    }
};

window.toggleProfileText = function(btn) {
    const parent = btn.parentNode;
    const shortText = parent.querySelector(".short-text");
    const fullText = parent.querySelector(".full-text");
    if (shortText && fullText) {
        const isCollapsed = fullText.style.display === "none";
        if (isCollapsed) {
            fullText.style.display = "inline";
            shortText.style.display = "none";
            btn.textContent = "收起";
        } else {
            fullText.style.display = "none";
            shortText.style.display = "inline";
            btn.textContent = "展开";
        }
    }
};

window.formatFileSize = function(bytes) {
    if (!bytes || bytes <= 0) return "";
    if (bytes < 1024) return bytes + "B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + "KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + "MB";
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + "GB";
};

window.formatDuration = function(seconds) {
    if (!seconds || seconds <= 0) return "";
    const sec = Math.floor(seconds);
    if (sec < 60) return sec + "s";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
};

/**
 * Match an input search term against a list of candidate options and resolve to the best matching option ID.
 * Hierarchy: Exact ID -> Exact Name -> Unique Match by (id, name, type) -> Fallback to raw trimmed term.
 *
 * @param {string} term Input string from user
 * @param {Array<{id: string, name?: string, type?: string}>} availableOptions Candidate list
 * @returns {string} Resolved option ID or cleaned custom term
 */
window.resolveOptionId = function(term, availableOptions = []) {
    if (!term) return '';
    const cleanTerm = String(term).trim();
    if (!cleanTerm) return '';
    const lower = cleanTerm.toLowerCase();

    if (!Array.isArray(availableOptions) || availableOptions.length === 0) {
        return cleanTerm;
    }

    // 1. Exact ID match (case-insensitive)
    const exactId = availableOptions.find(o => (o && o.id ? String(o.id).toLowerCase() : '') === lower);
    if (exactId && exactId.id) return exactId.id;

    // 2. Exact Name match (case-insensitive)
    const exactName = availableOptions.find(o => (o && o.name ? String(o.name).toLowerCase() : '') === lower);
    if (exactName && exactName.id) return exactName.id;

    // 3. Single matching candidate in options list
    const matched = availableOptions.filter(o => {
        if (!o) return false;
        const id = o.id ? String(o.id).toLowerCase() : '';
        const name = o.name ? String(o.name).toLowerCase() : '';
        const type = o.type ? String(o.type).toLowerCase() : '';
        return id.includes(lower) || name.includes(lower) || type.includes(lower);
    });

    if (matched.length === 1 && matched[0].id) {
        return matched[0].id;
    }

    // 4. Fallback to custom term string
    return cleanTerm;
};


