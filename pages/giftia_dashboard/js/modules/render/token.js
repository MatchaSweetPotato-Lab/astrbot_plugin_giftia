import { initializeScopedView } from '../filters.js';

let chartTabsInitialized = false;
let activeTokenTab = "group"; // group, model, type
let lastStats = null;
let tokenChartInstance = null;

// Public color mapping scheme
const colorsPalette = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#0ea5e9', '#14b8a6', '#f43f5e', '#6b7280'];
const keyColorsCache = {};
const staticTypeColors = {
    'reply': '#6366f1',
    'reply_tokens': '#6366f1',
    'decision': '#10b981',
    'decision_tokens': '#10b981',
    'image_caption': '#f59e0b',
    'image_caption_tokens': '#f59e0b',
    'sticker_analysis': '#14b8a6',
    'sticker_analysis_tokens': '#14b8a6',
    'audio_caption': '#ec4899',
    'audio_caption_tokens': '#ec4899',
    'passive_summary': '#8b5cf6',
    'passive_summary_tokens': '#8b5cf6',
    '其他': '#9ca3af',
    'Others': '#9ca3af'
};

function getColorForKey(key) {
    if (staticTypeColors[key]) {
        return staticTypeColors[key];
    }
    if (!keyColorsCache[key]) {
        const usedCount = Object.keys(keyColorsCache).length;
        keyColorsCache[key] = colorsPalette[usedCount % colorsPalette.length];
    }
    return keyColorsCache[key];
}

function getChartThemeColors() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const computedStyle = getComputedStyle(document.documentElement);
    const fontColor = computedStyle.getPropertyValue('--font-color').trim() || (isDark ? '#f8fafc' : '#0f172a');
    const fontSecondary = computedStyle.getPropertyValue('--font-secondary').trim() || (isDark ? '#94a3b8' : '#475569');
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.06)';
    return { fontColor, fontSecondary, gridColor };
}

export function updateChartTheme() {
    if (!tokenChartInstance) return;
    const { fontColor, fontSecondary, gridColor } = getChartThemeColors();
    if (tokenChartInstance.options.plugins && tokenChartInstance.options.plugins.legend) {
        tokenChartInstance.options.plugins.legend.labels.color = fontColor;
    }
    if (tokenChartInstance.options.scales) {
        if (tokenChartInstance.options.scales.x) {
            tokenChartInstance.options.scales.x.ticks.color = fontSecondary;
            tokenChartInstance.options.scales.x.grid.color = gridColor;
            tokenChartInstance.options.scales.x.grid.borderColor = gridColor;
        }
        if (tokenChartInstance.options.scales.y) {
            tokenChartInstance.options.scales.y.ticks.color = fontSecondary;
            tokenChartInstance.options.scales.y.grid.color = gridColor;
            tokenChartInstance.options.scales.y.grid.borderColor = gridColor;
        }
    }
    tokenChartInstance.update();
}

export async function initializeTokenStatsTab() {
    await initializeScopedView("tokenLogs");
    initChartTabs();
}

function initChartTabs() {
    if (chartTabsInitialized) return;
    const container = document.getElementById("token-chart-tabs-nav");
    if (!container) return;
    
    container.addEventListener("click", async (e) => {
        const btn = e.target.closest(".btn-tab");
        if (!btn) return;
        
        const tab = btn.getAttribute("data-tab");
        if (!tab) return;
        
        container.querySelectorAll(".btn-tab").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        
        activeTokenTab = tab;
        
        const subtitle = document.getElementById("token-breakdown-subtitle");
        if (subtitle) {
            const titles = {
                "group": "按会话/群聊占比明细",
                "model": "按供应商/模型占比明细",
                "type": "按操作类型占比明细"
            };
            subtitle.textContent = titles[tab] || "当前维度占比明细";
        }

        // If switching to group tab, reset group selection to all sessions
        const groupSelect = document.getElementById("token-group-id");
        if (tab === "group" && groupSelect && groupSelect.value !== "") {
            groupSelect.value = "";
            // This will reload data with unfiltered group
            await loadTokenStatsSummary();
        } else {
            renderChartAndProgressBars();
        }
    });
    
    chartTabsInitialized = true;
}

export async function loadTokenStatsSummary() {
    const botName = document.getElementById("token-bot-name").value;
    const groupId = document.getElementById("token-group-id").value;
    const timeRange = document.getElementById("token-time-range").value;

    const params = {};
    if (botName) params.bot_name = botName;
    if (groupId) params.group_or_user_id = groupId;
    if (timeRange) params.time_range = timeRange;

    try {
        const res = await window.apiGet("/token/stats", params);
        if (res.status === "success" && res.stats) {
            renderTokenStatsSummary(res.stats);
        }
    } catch (e) {
        console.error("加载 Token 统计概要失败", e);
    }
}

export function renderTokenStatsSummary(stats) {
    // Clear dynamic colors cache so rendering order remains deterministic and matches
    for (const k in keyColorsCache) delete keyColorsCache[k];

    lastStats = stats;
    const summary = stats.summary || {};

    // Total numbers
    document.getElementById("stat-total-tokens").textContent = Number(summary.total_tokens || 0).toLocaleString();
    document.getElementById("stat-tts-chars").textContent = Number(summary.total_chars_tts || 0).toLocaleString();

    // Set title breakdowns on hover
    const totalCard = document.getElementById("token-chart-legend-stats");
    if (totalCard) {
        totalCard.setAttribute("title", `输入 (Prompt): ${Number(summary.total_prompt_tokens || 0).toLocaleString()}\n输出 (Completion): ${Number(summary.total_completion_tokens || 0).toLocaleString()}`);
    }

    renderChartAndProgressBars();
}

function renderChartAndProgressBars() {
    if (!lastStats) return;

    // Clear dynamic colors cache so that the active tab's items always get deterministic colors starting from the first color in the palette
    for (const k in keyColorsCache) delete keyColorsCache[k];

    const stats = lastStats;
    const summary = stats.summary || {};
    const timeSeries = stats.time_series || { unit: 'day', timeline: [] };
    const timeline = timeSeries.timeline || [];

    // 1. Draw Line Chart
    const canvasEl = document.getElementById("token-line-chart");
    const ctx = canvasEl?.getContext("2d");
    if (ctx) {
        if (tokenChartInstance) {
            tokenChartInstance.destroy();
            tokenChartInstance = null;
        }

        const labels = timeline.map(item => item.time);
        const dimension = activeTokenTab;
        
        const keyTotals = {};
        timeline.forEach(item => {
            Object.entries(item[dimension] || {}).forEach(([k, val]) => {
                keyTotals[k] = (keyTotals[k] || 0) + val;
            });
        });

        const sortedKeys = Object.keys(keyTotals).sort((a, b) => {
            if (a === "其他") return 1;
            if (b === "其他") return -1;
            return keyTotals[b] - keyTotals[a];
        });

        const datasets = sortedKeys.map((key) => {
            const data = timeline.map(item => (item[dimension] && item[dimension][key]) || 0);
            
            // For the model dimension, display only the model name (remove the provider prefix)
            let label = key;
            if (dimension === "model" && key.includes("/")) {
                label = key.split("/").pop();
            } else if (dimension === "type") {
                const typeNameMap = {
                    'reply': '大模型回复',
                    'decision': '小模型判断',
                    'image_caption': '图片转述',
                    'sticker_analysis': '表情包分析',
                    'audio_caption': '音频转述',
                    'passive_summary': '被动总结'
                };
                label = typeNameMap[key] || key;
            }

            const color = getColorForKey(key);

            return {
                label: label,
                data: data,
                borderColor: color,
                backgroundColor: color + "1a", // ~10% opacity
                borderWidth: 2,
                pointRadius: timeline.length > 50 ? 0 : 3,
                pointHoverRadius: 5,
                tension: 0.3,
                fill: true,
                originalKey: key
            };
        });

        const formatLabel = (lbl, unit) => {
            if (unit === "hour") return `${lbl}:00`;
            if (unit === "day" && lbl.length === 10) return lbl.substring(5); // MM-DD
            return lbl;
        };

        const { fontColor, fontSecondary, gridColor } = getChartThemeColors();

        tokenChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels.map(l => formatLabel(l, timeSeries.unit)),
                datasets: datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: {
                            boxWidth: 10,
                            padding: 10,
                            font: {
                                size: 11
                            },
                            color: fontColor
                        }
                    },
                    tooltip: {
                        padding: 10,
                        callbacks: {
                            label: function(context) {
                                const dataset = context.dataset;
                                let originalKey = dataset.originalKey || dataset.label;
                                if (dimension === "type") {
                                    const typeNameMap = {
                                        'reply': '大模型回复',
                                        'decision': '小模型判断',
                                        'image_caption': '图片转述',
                                        'sticker_analysis': '表情包分析',
                                        'audio_caption': '音频转述',
                                        'passive_summary': '被动总结'
                                    };
                                    originalKey = typeNameMap[originalKey] || originalKey;
                                }
                                return ` ${originalKey}: ${context.raw.toLocaleString()} Tokens`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: gridColor,
                            borderColor: gridColor
                        },
                        ticks: {
                            color: fontSecondary,
                            maxTicksLimit: 12,
                            font: {
                                size: 10
                            }
                        }
                    },
                    y: {
                        grid: {
                            color: gridColor,
                            borderColor: gridColor
                        },
                        ticks: {
                            color: fontSecondary,
                            font: {
                                size: 10
                            },
                            callback: function(value) {
                                return value.toLocaleString();
                            }
                        }
                    }
                }
            }
        });
    }

    // 2. Render Active Progress Bars
    const container = document.getElementById("token-active-progress-bars");
    if (!container) return;

    if (activeTokenTab === "group") {
        const groups = stats.by_group || [];
        const maxGroupTokens = groups.reduce((sum, g) => sum + (g.total_tokens || 0), 0) || 1;
        if (groups.length === 0) {
            container.innerHTML = '<div style="font-size: 0.85rem; color: var(--font-secondary); text-align: center; padding: 20px 0;">暂无数据</div>';
        } else {
            container.innerHTML = groups.map((g) => {
                const tokens = g.total_tokens || 0;
                const pct = ((tokens / maxGroupTokens) * 100).toFixed(1);
                const gKey = g.group_or_user_id || '私聊/系统';
                const barColor = getColorForKey(gKey);
                return `
                    <div class="token-stat-row">
                        <div class="token-stat-info align-center">
                            <span class="token-stat-name monospace" title="${gKey}">${gKey}</span>
                            <span class="token-stat-value">${tokens.toLocaleString()} Tokens (${pct}%)</span>
                        </div>
                        <div class="progress-bar-container">
                            <div class="progress-bar-fill" style="width: ${pct}%; background-color: ${barColor};"></div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } else if (activeTokenTab === "model") {
        const models = stats.by_model || [];
        const maxModelTokens = models.reduce((sum, m) => sum + (m.total_tokens || 0), 0) || 1;
        if (models.length === 0) {
            container.innerHTML = '<div style="font-size: 0.85rem; color: var(--font-secondary); text-align: center; padding: 20px 0;">暂无数据</div>';
        } else {
            container.innerHTML = models.map((m) => {
                const tokens = m.total_tokens || 0;
                const pct = ((tokens / maxModelTokens) * 100).toFixed(1);
                let mainText = m.model_name || '未知';
                let subText = m.provider_id || '';
                if (subText && subText.includes('/')) {
                    const parts = subText.split('/');
                    subText = parts.slice(1).join('/');
                }
                const providerName = m.provider_id || mainText;
                const mKey = m.provider_id ? `${m.provider_id}/${m.model_name}` : m.model_name;
                const barColor = getColorForKey(mKey);
                return `
                    <div class="token-stat-row help-cursor" title="输入 (Prompt): ${Number(m.prompt_tokens || 0).toLocaleString()}\n输出 (Completion): ${Number(m.completion_tokens || 0).toLocaleString()}">
                        <div class="token-stat-info align-start">
                            <div class="token-stat-name-col">
                                <span class="token-stat-name bold" title="${providerName}">${mainText}</span>
                                ${subText ? `<span class="token-stat-subname" title="${subText}">${subText}</span>` : ''}
                            </div>
                            <span class="token-stat-value margin-left">${tokens.toLocaleString()} Tokens (${pct}%)</span>
                        </div>
                        <div class="progress-bar-container">
                            <div class="progress-bar-fill" style="width: ${pct}%; background-color: ${barColor};"></div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } else if (activeTokenTab === "type") {
        const categories = [
            { key: 'reply_tokens', name: '大模型回复', class: 'reply' },
            { key: 'decision_tokens', name: '小模型判断', class: 'decision' },
            { key: 'image_caption_tokens', name: '图片转述', class: 'image_caption' },
            { key: 'sticker_analysis_tokens', name: '表情包分析', class: 'sticker_analysis' },
            { key: 'audio_caption_tokens', name: '音频转述', class: 'audio_caption' },
            { key: 'passive_summary_tokens', name: '被动总结', class: 'passive_summary' }
        ];
        const activeCategories = categories.filter(cat => (summary[cat.key] || 0) > 0);
        const maxTypeTokens = activeCategories.reduce((sum, cat) => sum + (summary[cat.key] || 0), 0) || 1;
        if (activeCategories.length === 0) {
            container.innerHTML = '<div style="font-size: 0.85rem; color: var(--font-secondary); text-align: center; padding: 20px 0;">暂无数据</div>';
        } else {
            container.innerHTML = activeCategories.map(cat => {
                const tokens = summary[cat.key] || 0;
                const pct = ((tokens / maxTypeTokens) * 100).toFixed(1);
                let hoverTitle = "";
                if (cat.key === 'reply_tokens') {
                    hoverTitle = `输入 (Prompt): ${Number(summary.reply_prompt_tokens || 0).toLocaleString()}\n输出 (Completion): ${Number(summary.reply_completion_tokens || 0).toLocaleString()}`;
                } else if (cat.key === 'decision_tokens') {
                    hoverTitle = `输入 (Prompt): ${Number(summary.decision_prompt_tokens || 0).toLocaleString()}\n输出 (Completion): ${Number(summary.decision_completion_tokens || 0).toLocaleString()}`;
                }
                const barColor = getColorForKey(cat.class);
                return `
                    <div class="token-stat-row ${hoverTitle ? 'help-cursor' : ''}" ${hoverTitle ? `title="${hoverTitle}"` : ''}>
                        <div class="token-stat-info align-center">
                            <span class="token-stat-name">${cat.name}</span>
                            <span class="token-stat-value no-mono">${tokens.toLocaleString()} Tokens (${pct}%)</span>
                        </div>
                        <div class="progress-bar-container">
                            <div class="progress-bar-fill" style="width: ${pct}%; background-color: ${barColor};"></div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    }
}

export async function openTokenAutoCleanModal() {
    try {
        const res = await window.apiGet("/token/auto_clean/config");
        if (res.status === "success" && res.config) {
            document.getElementById("token-auto-clean-enabled").checked = res.config.enabled;
            document.getElementById("token-auto-clean-days").value = res.config.days;
            window.openModal("token-auto-clean-modal");
        } else {
            window.showToast("获取自动清理配置失败: " + (res.message || "未知错误"));
        }
    } catch (e) {
        window.showToast("获取自动清理配置失败: " + (e.message || e));
    }
}

export async function submitTokenAutoCleanConfig(silent = false) {
    const enabled = document.getElementById("token-auto-clean-enabled").checked;
    const days = parseInt(document.getElementById("token-auto-clean-days").value) || 365;

    try {
        const res = await window.apiPost("/token/auto_clean/config", { enabled, days });
        if (res.status === "success") {
            if (!silent) {
                window.showToast("已保存自动清理配置");
                window.closeModal("token-auto-clean-modal");
            }
        } else {
            if (!silent) {
                window.showToast("保存配置失败: " + (res.message || "未知错误"));
            }
        }
    } catch (e) {
        if (!silent) {
            window.showToast("保存配置失败: " + (e.message || e));
        }
    }
}

export function triggerAutoCleanTokenImmediately() {
    window.showConfirm("确认执行 Token 日志清理", "确认要按当前设定的天数立即清理过期的 Token 消耗日志吗？此操作无法撤销。", async () => {
        try {
            await submitTokenAutoCleanConfig(true);
            const res = await window.apiPost("/token/auto_clean/trigger", {});
            if (res && res.status === "success") {
                const count = res.count ?? 0;
                window.showToast(`Token 日志清理完成，共物理删除 ${count} 条过期记录`);
                window.closeModal("token-auto-clean-modal");
                await renderChartAndProgressBars();
            } else {
                window.showToast(`执行清理失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`执行清理出错: ${e.message}`);
        }
    });
}

export function openTokenClearModal() {
    const botName = document.getElementById("token-bot-name").value;
    const groupId = document.getElementById("token-group-id").value;
    const timeRange = document.getElementById("token-time-range").value;

    let confirmMsg = "警告：这将永久删除符合当前筛选条件的所有 Token 和合成字符的记录，无法恢复！";
    if (!botName && !groupId && !timeRange) {
        confirmMsg = "警告：您未设置任何筛选条件，这将永久删除所有历史 Token 和合成字符的记录，无法恢复！";
    }

    window.showConfirm("确认清空日志", confirmMsg, async () => {
        try {
            const res = await window.apiPost("/token/clear", {
                bot_name: botName,
                group_or_user_id: groupId,
                time_range: timeRange
            });
            if (res.status === "success") {
                window.showToast(res.message || "清空成功");
                await loadTokenStatsSummary();
            } else {
                window.showToast("清除日志失败: " + (res.message || "未知错误"));
            }
        } catch (e) {
            window.showToast("清除日志失败: " + (e.message || e));
        }
    });
}
