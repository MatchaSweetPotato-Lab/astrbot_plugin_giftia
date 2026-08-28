/**
 * Giftia Dashboard - Bot Management Module
 */

const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');
const showToast = (msg, type) => window.showToast ? window.showToast(msg, type) : alert(msg);
const sanitizeId = (str) => String(str || '').replace(/[^a-zA-Z0-9_-]/g, '_');

let stateBots = [];
let stateBotMetadata = {
    llm_providers: [],
    tts_providers: [],
    adapters: [],
    interactive_features: []
};
let currentEditingBotName = null;

// Tag Select Components Instances
let adaptersTagSelect = null;
let decProvidersTagSelect = null;
let replyProvidersTagSelect = null;

/**
 * Load bots and system metadata from backend.
 */
async function loadBotsData() {
    try {
        const json = await window.apiGet('/bots');
        if (json && json.status === 'success' && json.data) {
            stateBots = json.data.bots || [];
            stateBotMetadata = json.data.metadata || {};
            renderBotsGrid();
        } else {
            showToast(json?.message || '加载机器人配置失败', 'error');
        }
    } catch (err) {
        console.error('[Giftia Dashboard] loadBotsData error:', err);
        showToast('请求机器人数据异常', 'error');
    }
}

/**
 * Render Bot Cards Grid.
 */
function renderBotsGrid() {
    const container = document.getElementById('bots-grid');
    if (!container) return;

    if (!stateBots || stateBots.length === 0) {
        container.innerHTML = `
            <div class="empty-state card" style="grid-column: 1 / -1; padding: 40px; text-align: center;">
                <p style="color: var(--font-secondary); margin-bottom: 15px;">暂无创设的机器人配置</p>
                <button class="btn btn-primary" onclick="openBotEditModal()">+ 创建第一个机器人</button>
            </div>
        `;
        return;
    }

    container.innerHTML = stateBots.map(bot => {
        const enabled = bot.enabled !== false;
        const name = escapeHtml(bot.name || '未命名');
        const nickname = escapeHtml(bot.nickname || name);
        const safeDomId = sanitizeId(bot.name);
        const adapters = bot.adapter_ids || [];
        const decEnabled = bot.decision_conf?.enabled !== false;
        const replyEnabled = bot.llm_reply_conf?.enabled !== false;
        const ttsEnabled = bot.tts_config?.enabled === true;
        const activeFeaturesCount = (bot.enabled_interactive_features || []).length;

        return `
            <div class="bot-card card ${enabled ? '' : 'bot-disabled'}" style="position: relative; display: flex; flex-direction: column; gap: 12px; padding: 18px;">
                <div class="flex-between" style="align-items: center;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div class="bot-avatar-badge" style="width: 38px; height: 38px; border-radius: 50%; background: var(--theme-primary-gradient, linear-gradient(135deg, #6366f1, #a855f7)); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 1.1rem;">
                            ${nickname.charAt(0).toUpperCase()}
                        </div>
                        <div>
                            <div style="font-weight: 600; font-size: 1.05rem; color: var(--font-primary); display: flex; align-items: center; gap: 6px;">
                                ${name}
                                ${name !== nickname ? `<span style="font-size: 0.8rem; font-weight: normal; color: var(--font-secondary);">(${nickname})</span>` : ''}
                            </div>
                            <div style="font-size: 0.78rem; color: var(--font-secondary); margin-top: 2px;">
                                适配器: ${adapters.length > 0 ? adapters.map(a => `<code class="tag-badge">${escapeHtml(a)}</code>`).join(' ') : '<span class="muted-text">全部适用</span>'}
                            </div>
                        </div>
                    </div>
                    <div class="switch-container" title="${enabled ? '暂停该机器人' : '启用该机器人'}">
                        <input type="checkbox" class="switch-checkbox" id="toggle-bot-${safeDomId}" ${enabled ? 'checked' : ''} onchange="toggleBotStatus('${escapeHtml(bot.name)}', this.checked)">
                        <label for="toggle-bot-${safeDomId}" class="switch-label"></label>
                    </div>
                </div>

                <div class="bot-features-summary" style="display: flex; flex-wrap: wrap; gap: 6px; font-size: 0.78rem; margin: 4px 0;">
                    <span class="badge ${decEnabled ? 'badge-success' : 'badge-secondary'}">
                        小模型判断: ${decEnabled ? '开启' : '关闭'}
                    </span>
                    <span class="badge ${replyEnabled ? 'badge-success' : 'badge-secondary'}">
                        大模型回复: ${replyEnabled ? '开启' : '关闭'}
                    </span>
                    <span class="badge badge-info">
                        人格: ${escapeHtml(bot.llm_reply_conf?.persona_id || 'default')}
                    </span>
                    <span class="badge ${bot.decision_conf?.at_behavior && bot.decision_conf.at_behavior !== 'force_reply' ? 'badge-info' : 'badge-secondary'}">
                        ${bot.decision_conf?.at_behavior === 'activate_and_decide' ? '@ 激活并判断' : (bot.decision_conf?.at_behavior === 'decide_in_window_force_outside' ? '@ 窗口内判断' : '@ 强制回复')}
                    </span>
                    <span class="badge ${ttsEnabled ? 'badge-info' : 'badge-secondary'}">
                        TTS 语音: ${ttsEnabled ? escapeHtml(bot.tts_config?.provider_type || '已开启') : '未开启'}
                    </span>
                    <span class="badge badge-purple">
                        交互标签: ${activeFeaturesCount} 项
                    </span>
                </div>

                <div class="bot-card-actions flex-between" style="margin-top: 6px; padding-top: 10px; border-top: 1px solid var(--border-color, rgba(255,255,255,0.08));">
                    <button class="btn btn-small btn-secondary" onclick="openBotEditModal('${escapeHtml(bot.name)}')">
                        ✏️ 编辑配置
                    </button>
                    <button class="btn btn-small btn-danger-link" style="color: var(--color-danger, #ef4444); border: none; background: transparent; cursor: pointer;" onclick="deleteBotConfig('${escapeHtml(bot.name)}')">
                        删除
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

/**
 * Toggle Bot Enabled Status.
 */
async function toggleBotStatus(botName, enabled) {
    try {
        const json = await window.apiPost('/bots/toggle', { name: botName, enabled: enabled });
        if (json && json.status === 'success') {
            showToast(`机器人 ${botName} 已${enabled ? '启用' : '暂停'}`, 'success');
            await loadBotsData();
        } else {
            showToast(json?.message || '更新状态失败', 'error');
            await loadBotsData();
        }
    } catch (err) {
        showToast('网络错误，修改状态失败', 'error');
        await loadBotsData();
    }
}

/**
 * Delete Bot.
 */
async function deleteBotConfig(botName) {
    if (!confirm(`确定要彻底删除机器人 "${botName}" 的配置吗？`)) {
        return;
    }
    try {
        const json = await window.apiPost('/bots/delete', { name: botName });
        if (json && json.status === 'success') {
            showToast(json.message || '成功删除机器人', 'success');
            await loadBotsData();
        } else {
            showToast(json?.message || '删除失败', 'error');
        }
    } catch (err) {
        showToast('删除请求异常', 'error');
    }
}

/**
 * Open Modal to Create or Edit a Bot.
 */
function openBotEditModal(botName = null) {
    currentEditingBotName = botName;
    const modal = document.getElementById('bot-edit-modal');
    const title = document.getElementById('bot-modal-title');
    if (!modal) return;

    const bot = stateBots.find(b => b.name === botName) || {
        enabled: true,
        name: '',
        nickname: '',
        adapter_ids: [],
        decision_conf: {
            enabled: true,
            provider_ids: [],
            group_whitelist: [],
            decision_prompt: '',
            reply_active_window: 10,
            proactive_probability: 0,
            keyword_trigger_enabled: false,
            keyword_rules: [],
            keyword_default_probability: 100,
            at_behavior: 'force_reply'
        },
        llm_reply_conf: {
            enabled: true,
            provider_ids: [],
            provider_selection_mode: 'fallback',
            persona_id: 'default'
        },
        tts_config: {
            enabled: false,
            provider_type: 'minimax',
            zh_provider_id: '',
            en_provider_id: '',
            ja_provider_id: '',
            default_language: '中文',
            replace_in_message: false,
            signature_voices: []
        },
        enabled_interactive_features: (stateBotMetadata.interactive_features || [])
            .map(f => typeof f === 'object' && f !== null && f.key ? f.key : f)
            .filter(key => key !== 'leave')
    };

    title.innerText = botName ? `编辑机器人: ${botName}` : '新增机器人配置';

    // 1. Basic Info
    document.getElementById('bot-form-name').value = bot.name || '';
    document.getElementById('bot-form-nickname').value = bot.nickname || '';

    // Destroy existing TagSelect instances before re-creating
    if (adaptersTagSelect && typeof adaptersTagSelect.destroy === 'function') adaptersTagSelect.destroy();
    if (decProvidersTagSelect && typeof decProvidersTagSelect.destroy === 'function') decProvidersTagSelect.destroy();
    if (replyProvidersTagSelect && typeof replyProvidersTagSelect.destroy === 'function') replyProvidersTagSelect.destroy();

    // Initialize Tag Select for Adapter IDs
    adaptersTagSelect = new TagSelectComponent('bot-form-adapters', {
        placeholder: '点击或检索选择需绑定的适配器...',
        availableOptions: stateBotMetadata.adapters || [],
        selectedValues: bot.adapter_ids || []
    });

    // 2. Decision Conf
    document.getElementById('bot-form-dec-enabled').checked = bot.decision_conf?.enabled !== false;

    // Initialize Priority Select for Decision Providers
    decProvidersTagSelect = new PrioritySelectComponent('bot-form-dec-providers', {
        placeholder: '检索或输入小模型提供商 (按回车添加)...',
        availableOptions: stateBotMetadata.llm_providers || [],
        selectedValues: bot.decision_conf?.provider_ids || []
    });

    const atBehaviorEl = document.getElementById('bot-form-dec-at-behavior');
    if (atBehaviorEl) atBehaviorEl.value = bot.decision_conf?.at_behavior || 'force_reply';
    document.getElementById('bot-form-dec-whitelist').value = (bot.decision_conf?.group_whitelist || []).join(', ');
    document.getElementById('bot-form-dec-window').value = bot.decision_conf?.reply_active_window ?? 10;
    document.getElementById('bot-form-dec-proactive').value = bot.decision_conf?.proactive_probability ?? 0;
    document.getElementById('bot-form-dec-prompt').value = bot.decision_conf?.decision_prompt || '';
    document.getElementById('bot-form-dec-kw-enabled').checked = bot.decision_conf?.keyword_trigger_enabled === true;
    document.getElementById('bot-form-dec-kw-rules').value = (bot.decision_conf?.keyword_rules || []).join(', ');
    document.getElementById('bot-form-dec-kw-prob').value = bot.decision_conf?.keyword_default_probability ?? 100;

    // 3. Reply Conf
    document.getElementById('bot-form-reply-enabled').checked = bot.llm_reply_conf?.enabled !== false;

    // Initialize Priority Select for Reply Providers
    replyProvidersTagSelect = new PrioritySelectComponent('bot-form-reply-providers', {
        placeholder: '检索或输入大模型回复提供商 (按回车添加)...',
        availableOptions: stateBotMetadata.llm_providers || [],
        selectedValues: bot.llm_reply_conf?.provider_ids || []
    });

    document.getElementById('bot-form-reply-mode').value = bot.llm_reply_conf?.provider_selection_mode || 'fallback';
    renderPersonaSelectOptions(bot.llm_reply_conf?.persona_id || 'default');

    // 4. TTS Conf
    document.getElementById('bot-form-tts-enabled').checked = bot.tts_config?.enabled === true;
    document.getElementById('bot-form-tts-type').value = bot.tts_config?.provider_type || 'minimax';
    document.getElementById('bot-form-tts-replace-msg').checked = bot.tts_config?.replace_in_message === true;
    renderTtsLangRulesList(bot.tts_config?.language_provider_map || []);
    renderSignatureVoicesList(bot.tts_config?.signature_voices || []);
    loadUploadedVoicesPool();


    // 5. Interactive Features
    renderInteractiveFeaturesCheckboxes(bot.enabled_interactive_features || []);

    // Switch to first sub-tab
    switchBotModalSubTab('subtab-identity');

    if (typeof window.openModal === 'function') {
        window.openModal('bot-edit-modal');
    } else {
        modal.classList.add('show');
    }
}

/**
 * Render persona selection dropdown in bot edit modal.
 */
function renderPersonaSelectOptions(selectedPersonaId = 'default') {
    const el = document.getElementById('bot-form-reply-persona');
    if (!el) return;

    const personas = stateBotMetadata.personas || [];
    let optionsHtml = '';
    let found = false;

    personas.forEach(p => {
        const pName = p.name || 'default';
        const isSelected = pName === selectedPersonaId ? 'selected' : '';
        if (pName === selectedPersonaId) found = true;
        let toolCountInfo = p.tools === null || p.tools === undefined ? '全能力无限制' : (Array.isArray(p.tools) ? `${p.tools.length} 个可用工具` : '未知');
        let label = `${pName} (${toolCountInfo})`;
        optionsHtml += `<option value="${escapeHtml(pName)}" ${isSelected}>${escapeHtml(label)}</option>`;
    });

    if (selectedPersonaId && !found) {
        optionsHtml += `<option value="${escapeHtml(selectedPersonaId)}" selected>${escapeHtml(selectedPersonaId)} (未在系统注册)</option>`;
    }

    el.innerHTML = optionsHtml;
}

/**
 * Render single-select TTS provider dropdown.
 */
function renderSingleProviderSelect(selectId, providers, selectedId) {
    const el = document.getElementById(selectId);
    if (!el) return;

    let optionsHtml = `<option value="">-- 未选择 --</option>`;
    let found = false;

    (providers || []).forEach(p => {
        const selected = p.id === selectedId ? 'selected' : '';
        if (p.id === selectedId) found = true;

        let label = p.name || p.id;
        if (p.name && p.id && p.name !== p.id && !p.name.includes(p.id)) {
            label = `${p.name} (${p.id})`;
        }

        optionsHtml += `<option value="${escapeHtml(p.id)}" ${selected}>${escapeHtml(label)}</option>`;
    });

    if (selectedId && !found) {
        optionsHtml += `<option value="${escapeHtml(selectedId)}" selected>${escapeHtml(selectedId)}</option>`;
    }

    el.innerHTML = optionsHtml;
}

/**
 * Render Interactive XML Features checkboxes grid.
 */
function renderInteractiveFeaturesCheckboxes(selectedFeatures) {
    const container = document.getElementById('bot-form-interactive-grid');
    if (!container) return;
    const allFeatures = stateBotMetadata.interactive_features || [];
    const activeKeys = (selectedFeatures || []).map(sf => String(sf).split('(')[0].trim());

    container.innerHTML = allFeatures.map(feat => {
        const key = typeof feat === 'object' && feat.key ? feat.key : String(feat);
        const label = typeof feat === 'object' && feat.label ? feat.label : feat;
        const note = typeof feat === 'object' && feat.note ? feat.note : '';
        const isChecked = activeKeys.includes(key);

        const id = `feat-${key.replace(/[^a-zA-Z0-9]/g, '_')}`;
        const noteHtml = note ? `<span style="font-size: 0.75rem; color: var(--font-secondary); font-weight: normal;">(${escapeHtml(note)})</span>` : '';

        return `
            <label class="feature-checkbox-card" for="${id}" style="display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--bg-tertiary, rgba(255,255,255,0.04)); border: 1px solid var(--border-color, rgba(255,255,255,0.08)); border-radius: 6px; cursor: pointer;">
                <input type="checkbox" id="${id}" value="${escapeHtml(key)}" class="bot-feature-cb" ${isChecked ? 'checked' : ''}>
                <span style="font-size: 0.85rem; color: var(--font-primary);">${escapeHtml(label)} ${noteHtml}</span>
            </label>
        `;
    }).join('');
}

let stateUploadedVoices = [];

/**
 * Load uploaded voices pool from backend.
 */
async function loadUploadedVoicesPool() {
    const container = document.getElementById('bot-uploaded-voices-pool');
    if (!container) return;

    try {
        const json = await window.apiGet('/bots/voice/list');
        if (json && json.status === 'success' && Array.isArray(json.data)) {
            stateUploadedVoices = json.data;
        } else {
            stateUploadedVoices = [];
        }
    } catch (e) {
        console.warn('Failed to fetch voice pool:', e);
        stateUploadedVoices = [];
    }

    renderVoicesPoolUI();
    updateVoiceTagSelectOptions();
}

function updateVoiceTagSelectOptions() {
    const availableOptions = (stateUploadedVoices || []).map(v => ({
        id: v.rel_path,
        name: v.filename
    }));

    document.querySelectorAll('#bot-form-voices-list .voice-rule-row').forEach(row => {
        if (row.tagSelectInstance) {
            row.tagSelectInstance.setAvailableOptions(availableOptions);
        }
    });
}

function renderVoicesPoolUI() {
    const container = document.getElementById('bot-uploaded-voices-pool');
    if (!container) return;

    if (!stateUploadedVoices || stateUploadedVoices.length === 0) {
        container.innerHTML = `<span style="color: var(--font-muted); font-size: 0.85rem; padding: 4px;">暂无已上传的语音文件，点击右上方“上传音频”批量上传...</span>`;
        return;
    }

    container.innerHTML = stateUploadedVoices.map(v => {
        const relPath = escapeHtml(v.rel_path);
        const fileName = escapeHtml(v.filename);
        return `
            <div class="voice-pool-chip" style="display: inline-flex; align-items: center; gap: 8px; padding: 4px 10px; background: var(--bg-tertiary, rgba(255,255,255,0.04)); border: 1px solid var(--border-color, rgba(255,255,255,0.1)); border-radius: 6px; font-size: 0.82rem; color: var(--font-primary);">
                <button type="button" onclick="playVoiceAudioPreview('${relPath}')" title="试听音频" style="background: rgba(24, 144, 255, 0.12); border: 1px solid rgba(24, 144, 255, 0.25); color: #40a9ff; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; transition: all 0.2s;">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 试听
                </button>
                <span title="${relPath}" style="max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500;">${fileName}</span>
                <button type="button" onclick="deleteVoiceFileFromPool(this, '${relPath}', '${fileName}')" title="彻底删除此音频文件" style="background: rgba(255, 77, 79, 0.1); border: 1px solid rgba(255, 77, 79, 0.25); color: #ff7875; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; transition: all 0.2s;">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> 删除
                </button>
            </div>
        `;
    }).join('');
}

async function deleteVoiceFileFromPool(btnEl, relPath, fileName) {
    if (!relPath) return;

    if (!btnEl.dataset.confirming) {
        btnEl.dataset.confirming = 'true';
        btnEl.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> 确认删除`;
        btnEl.style.background = '#ff4d4f';
        btnEl.style.color = '#ffffff';
        btnEl.style.borderColor = '#ff4d4f';

        setTimeout(() => {
            if (btnEl && btnEl.dataset.confirming) {
                delete btnEl.dataset.confirming;
                btnEl.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> 删除`;
                btnEl.style.background = 'rgba(255, 77, 79, 0.1)';
                btnEl.style.color = '#ff7875';
                btnEl.style.borderColor = 'rgba(255, 77, 79, 0.25)';
            }
        }, 3000);
        return;
    }

    delete btnEl.dataset.confirming;

    try {
        const json = await window.apiPost('/bots/voice/delete', { rel_path: relPath });
        if (json && json.status === 'success') {
            showToast(`语音文件 "${fileName}" 已彻底删除`, 'success');
            await loadUploadedVoicesPool();
        } else {
            showToast(json?.message || '删除语音文件失败', 'error');
        }
    } catch (e) {
        showToast('删除语音文件请求异常', 'error');
    }
}

async function playVoiceAudioPreview(relPath) {
    if (!relPath) return;
    try {
        const json = await window.apiPost('/bots/voice/file/b64', { rel_path: relPath });
        if (json && json.status === 'success' && json.data && json.data.b64) {
            const audio = new Audio(json.data.b64);
            audio.play().catch(e => {
                showToast(`试听失败: ${e.message}`, 'info');
            });
        } else {
            showToast(json?.message || '无法获取试听音频数据', 'error');
        }
    } catch (e) {
        showToast('试听音频请求异常', 'error');
    }
}

/**
 * Upload multiple voice files helper.
 */
async function uploadVoiceFilesForModal(input) {
    const files = input.files;
    if (!files || files.length === 0) return;

    let successCnt = 0;
    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        try {
            const b64 = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = e => resolve(e.target.result);
                reader.onerror = reject;
                reader.readAsDataURL(file);
            });

            const json = await window.apiPost('/bots/voice/upload', {
                filename: file.name,
                content: b64
            });

            if (json && json.status === 'success') {
                successCnt++;
            }
        } catch (e) {
            console.error('Failed uploading file:', file.name, e);
        }
    }

    if (successCnt > 0) {
        showToast(`成功上传 ${successCnt} 个语音文件！`, 'success');
        await loadUploadedVoicesPool();
    } else {
        showToast('语音文件上传失败', 'error');
    }
    input.value = '';
}

/**
 * Render signature voice rule inputs.
 */
function renderSignatureVoicesList(voices) {
    const container = document.getElementById('bot-form-voices-list');
    if (!container) return;

    container.querySelectorAll('.voice-rule-row').forEach(row => {
        if (row.tagSelectInstance && typeof row.tagSelectInstance.destroy === 'function') {
            row.tagSelectInstance.destroy();
        }
    });

    container.innerHTML = '';
    (voices || []).forEach(v => {
        addVoiceRuleRow(v);
    });

    if (!voices || voices.length === 0) {
        addVoiceRuleRow('');
    }
}

function addVoiceRuleRow(initialValue = '') {
    const container = document.getElementById('bot-form-voices-list');
    if (!container) return;

    let audiosStr = '';
    let textsStr = '';

    if (initialValue && typeof initialValue === 'string') {
        if (initialValue.includes(':') || initialValue.includes('：')) {
            const delim = initialValue.includes(':') ? ':' : '：';
            const parts = initialValue.split(delim);
            audiosStr = parts[0].trim();
            textsStr = parts.slice(1).join(delim).trim();
        } else {
            audiosStr = initialValue.trim();
        }
    }

    const selectedAudios = audiosStr ? audiosStr.split(/[,，|;]/).map(s => s.trim()).filter(s => s) : [];

    const div = document.createElement('div');
    div.className = 'voice-rule-row';
    div.style.cssText = 'display: flex; gap: 10px; margin-bottom: 8px; align-items: flex-start; background: var(--card-bg, rgba(255,255,255,0.02)); padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border-color, rgba(255,255,255,0.08));';

    const tagSelectContainerId = 'voice-rule-tagselect-' + Math.random().toString(36).substr(2, 9);

    div.innerHTML = `
        <div style="flex: 1.4; display: flex; gap: 6px; align-items: center; min-width: 0;">
            <span style="font-size: 0.82rem; font-weight: 600; color: var(--font-secondary); white-space: nowrap; margin-top: 6px;">音频:</span>
            <div id="${tagSelectContainerId}" style="flex: 1; min-width: 0;"></div>
        </div>
        <div style="flex: 1; display: flex; gap: 6px; align-items: center; margin-top: 4px;">
            <span style="font-size: 0.82rem; font-weight: 600; color: var(--font-secondary); white-space: nowrap;">触发词:</span>
            <input type="text" class="form-input voice-rule-texts-input" value="${escapeHtml(textsStr)}" placeholder="填入触发词 (如: 你好, 嗨)" style="flex: 1;">
        </div>
        <button type="button" class="btn btn-secondary btn-small" onclick="removeVoiceRuleRow(this)" style="color: #ff4d4f; margin-top: 4px;">删除</button>
    `;

    container.appendChild(div);

    const availableOptions = (stateUploadedVoices || []).map(v => ({
        id: v.rel_path,
        name: v.filename
    }));

    const tagSelectInstance = new TagSelectComponent(tagSelectContainerId, {
        placeholder: '点击或检索选择音频...',
        availableOptions: availableOptions,
        selectedValues: selectedAudios
    });

    div.tagSelectInstance = tagSelectInstance;
}

function removeVoiceRuleRow(btn) {
    const row = btn.closest('.voice-rule-row');
    if (row) {
        if (row.tagSelectInstance && typeof row.tagSelectInstance.destroy === 'function') {
            row.tagSelectInstance.destroy();
        }
        row.remove();
    }
}


/**
 * Render dynamic TTS language and provider mapping list.
 */
function renderTtsLangRulesList(langMap) {
    const container = document.getElementById('bot-form-tts-lang-list');
    if (!container) return;
    container.innerHTML = '';

    let items = Array.isArray(langMap) ? langMap : [];
    if (items.length === 0) {
        items = [
            { language: '中文', provider_id: '' },
            { language: '英文', provider_id: '' },
            { language: '日文', provider_id: '' }
        ];
    }

    items.forEach((item, index) => addTtsLangRuleRow(item, index === 0));
    updateTtsLangBadges();
}

function addTtsLangRuleRow(initialItem = null, isFirst = false) {
    const container = document.getElementById('bot-form-tts-lang-list');
    if (!container) return;

    const initialLang = initialItem ? (initialItem.language || initialItem.lang || '') : '';
    const initialProvider = initialItem ? (initialItem.provider_id || initialItem.provider || '') : '';

    const div = document.createElement('div');
    div.className = 'tts-lang-rule-row';
    div.style.cssText = 'display: flex; gap: 10px; margin-bottom: 8px; align-items: center; background: var(--card-bg, rgba(255,255,255,0.02)); padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border-color, rgba(255,255,255,0.08));';

    const providers = stateBotMetadata.tts_providers || [];
    const optionsHtml = `<option value="">-- 选择 TTS 供应商 --</option>` + providers.map(p => `
        <option value="${escapeHtml(p.id)}" ${p.id === initialProvider ? 'selected' : ''}>${escapeHtml(p.name)} (${escapeHtml(p.type || p.id)})</option>
    `).join('');

    div.innerHTML = `
        <div style="width: 120px; display: flex; align-items: center; gap: 6px;">
            <select class="form-input tts-lang-name-select" style="width: 100%;">
                <option value="中文" ${initialLang === '中文' || initialLang === 'zh-CN' ? 'selected' : ''}>中文</option>
                <option value="英文" ${initialLang === '英文' || initialLang === 'en-US' ? 'selected' : ''}>英文</option>
                <option value="日文" ${initialLang === '日文' || initialLang === 'ja-JP' ? 'selected' : ''}>日文</option>
            </select>
        </div>
        <div style="flex: 1; display: flex; align-items: center; gap: 6px;">
            <select class="form-input tts-lang-provider-select" style="flex: 1;">
                ${optionsHtml}
            </select>
        </div>
        <span class="tts-default-badge badge badge-info" style="display: ${isFirst ? 'inline-flex' : 'none'}; font-size: 0.75rem; white-space: nowrap;">首选/默认</span>
        <button type="button" class="btn btn-secondary btn-small" onclick="removeTtsLangRuleRow(this)" style="color: #ff4d4f;">删除</button>
    `;

    container.appendChild(div);
    updateTtsLangBadges();
}

function removeTtsLangRuleRow(btn) {
    btn.closest('.tts-lang-rule-row')?.remove();
    updateTtsLangBadges();
}

function updateTtsLangBadges() {
    const rows = document.querySelectorAll('#bot-form-tts-lang-list .tts-lang-rule-row');
    rows.forEach((row, idx) => {
        const badge = row.querySelector('.tts-default-badge');
        if (badge) {
            badge.style.display = (idx === 0) ? 'inline-flex' : 'none';
        }
    });
}

/**
 * Save Bot Config from Modal Form.
 */
async function saveBotFromModal() {
    const name = document.getElementById('bot-form-name').value.trim();
    if (!name) {
        showToast('请输入机器人唯一名称 (Name)', 'error');
        switchBotModalSubTab('subtab-identity');
        return;
    }

    const adapterIds = adaptersTagSelect ? adaptersTagSelect.getValues() : [];
    const decSelectedProviders = decProvidersTagSelect ? decProvidersTagSelect.getValues() : [];
    const replySelectedProviders = replyProvidersTagSelect ? replyProvidersTagSelect.getValues() : [];

    const activeFeatures = Array.from(document.querySelectorAll('.bot-feature-cb:checked')).map(cb => cb.value);

    const signatureVoices = [];
    document.querySelectorAll('#bot-form-voices-list .voice-rule-row').forEach(row => {
        const audios = row.tagSelectInstance ? row.tagSelectInstance.getValues().join(', ') : '';
        const texts = (row.querySelector('.voice-rule-texts-input')?.value || '').trim();
        if (audios || texts) {
            if (audios && texts) {
                signatureVoices.push(`${audios} : ${texts}`);
            } else if (audios) {
                signatureVoices.push(audios);
            }
        }
    });

    const languageProviderMap = [];
    document.querySelectorAll('#bot-form-tts-lang-list .tts-lang-rule-row').forEach(row => {
        const lang = (row.querySelector('.tts-lang-name-select')?.value || '').trim();
        const providerId = (row.querySelector('.tts-lang-provider-select')?.value || '').trim();
        if (lang && providerId) {
            languageProviderMap.push({
                language: lang,
                provider_id: providerId
            });
        }
    });


    const botConfig = {
        _original_name: currentEditingBotName,
        enabled: currentEditingBotName ? (stateBots.find(b => b.name === currentEditingBotName)?.enabled !== false) : true,
        name: name,
        nickname: document.getElementById('bot-form-nickname').value.trim() || name,
        adapter_ids: adapterIds,
        decision_conf: {
            enabled: document.getElementById('bot-form-dec-enabled').checked,
            provider_ids: decSelectedProviders,
            group_whitelist: document.getElementById('bot-form-dec-whitelist').value.split(',').map(s => s.trim()).filter(s => s),
            decision_prompt: document.getElementById('bot-form-dec-prompt').value,
            reply_active_window: parseInt(document.getElementById('bot-form-dec-window').value || 10),
            proactive_probability: parseInt(document.getElementById('bot-form-dec-proactive').value || 0),
            keyword_trigger_enabled: document.getElementById('bot-form-dec-kw-enabled').checked,
            keyword_rules: document.getElementById('bot-form-dec-kw-rules').value.split(',').map(s => s.trim()).filter(s => s),
            keyword_default_probability: parseInt(document.getElementById('bot-form-dec-kw-prob').value || 100),
            at_behavior: document.getElementById('bot-form-dec-at-behavior')?.value || 'force_reply',
        },
        llm_reply_conf: {
            enabled: document.getElementById('bot-form-reply-enabled').checked,
            provider_ids: replySelectedProviders,
            provider_selection_mode: document.getElementById('bot-form-reply-mode').value,
            persona_id: document.getElementById('bot-form-reply-persona')?.value || 'default',
        },
        tts_config: {
            enabled: document.getElementById('bot-form-tts-enabled').checked,
            provider_type: document.getElementById('bot-form-tts-type').value,
            language_provider_map: languageProviderMap,
            replace_in_message: document.getElementById('bot-form-tts-replace-msg').checked,
            signature_voices: signatureVoices,
        },
        enabled_interactive_features: activeFeatures,
    };


    try {
        const json = await window.apiPost('/bots/save', botConfig);
        if (json && json.status === 'success') {
            showToast('机器人配置保存成功！', 'success');
            if (typeof window.closeModal === 'function') {
                window.closeModal('bot-edit-modal');
            } else {
                document.getElementById('bot-edit-modal')?.classList.remove('show');
            }
            await loadBotsData();
        } else {
            showToast(json?.message || '保存失败', 'error');
        }
    } catch (err) {
        showToast('保存机器人配置网络错误', 'error');
    }
}

/**
 * Switch sub-tabs inside Bot Modal.
 */
function switchBotModalSubTab(tabId) {
    document.querySelectorAll('#bot-edit-modal .card-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.subtab === tabId);
    });
    document.querySelectorAll('#bot-edit-modal .bot-subtab-content').forEach(content => {
        content.style.display = content.id === tabId ? 'block' : 'none';
    });
}

// Bind to window object for inline HTML onclick event handlers
window.loadBotsData = loadBotsData;
window.openBotEditModal = openBotEditModal;
window.saveBotFromModal = saveBotFromModal;
window.toggleBotStatus = toggleBotStatus;
window.deleteBotConfig = deleteBotConfig;
window.switchBotModalSubTab = switchBotModalSubTab;
window.addVoiceRuleRow = addVoiceRuleRow;
window.removeVoiceRuleRow = removeVoiceRuleRow;
window.uploadVoiceFilesForModal = uploadVoiceFilesForModal;
window.loadUploadedVoicesPool = loadUploadedVoicesPool;
window.playVoiceAudioPreview = playVoiceAudioPreview;
window.deleteVoiceFileFromPool = deleteVoiceFileFromPool;
window.addTtsLangRuleRow = addTtsLangRuleRow;
window.removeTtsLangRuleRow = removeTtsLangRuleRow;
window.renderTtsLangRulesList = renderTtsLangRulesList;

export {
    loadBotsData,
    openBotEditModal,
    saveBotFromModal,
    toggleBotStatus,
    deleteBotConfig,
    switchBotModalSubTab,
    addVoiceRuleRow,
    removeVoiceRuleRow,
    uploadVoiceFilesForModal,
    loadUploadedVoicesPool,
    playVoiceAudioPreview,
    deleteVoiceFileFromPool,
    addTtsLangRuleRow,
    removeTtsLangRuleRow,
    renderTtsLangRulesList
};

