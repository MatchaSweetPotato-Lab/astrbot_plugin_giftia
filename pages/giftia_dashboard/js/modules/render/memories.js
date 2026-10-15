import { state } from '../state.js';
import { MEMORY_KINDS, MEMORY_STATUSES } from '../memory_types.js';

let loadVersion = 0;

export async function loadMemories() {
    const version = ++loadVersion;
    const listContainer = document.getElementById('memory-list');
    listContainer.innerHTML = '<div class="loading-row"><span class="loader"></span> 加载数据中...</div>';
    const params = {
        page: state.pagination.memories.page,
        limit: state.pagination.memories.limit,
        ...window.GiftiaApp.getScopedFilterParams('memories'),
        group_or_user_id: document.getElementById('memory-group-id').value
    };
    const selection = JSON.stringify(params);
    const isCurrent = () => version === loadVersion && selection === JSON.stringify({
        page: state.pagination.memories.page,
        limit: state.pagination.memories.limit,
        ...window.GiftiaApp.getScopedFilterParams('memories'),
        group_or_user_id: document.getElementById('memory-group-id').value
    });
    if (!params.bot_name || !params.group_or_user_id) {
        state.pagination.memories.total = 0;
        listContainer.innerHTML = '<div class="no-data-row">当前筛选下暂无记忆</div>';
        window.renderPagination('memory-pagination', state.pagination.memories, () => {});
        return;
    }

    try {
        const res = await window.apiGet('/memories', params);
        if (!isCurrent()) return;
        if (res.status !== 'success' || !res.data) throw new Error(res.message || '请求失败');
        state.pagination.memories.total = res.data.total;
        renderMemories(res.data.items, res.data.user_id_to_name || {});
        window.renderPagination('memory-pagination', state.pagination.memories, (page) => {
            state.pagination.memories.page = page;
            loadMemories();
        });
    } catch (e) {
        if (isCurrent()) {
            listContainer.innerHTML = `<div class="no-data-row">加载数据失败: ${window.escapeHtml(e.message)}</div>`;
        }
    }
}

export function renderMemories(items, userIdToName = {}) {
    const container = document.getElementById('memory-list');
    if (!items?.length) {
        container.innerHTML = '<div class="no-data-row">当前筛选下暂无记忆</div>';
        return;
    }
    const escape = value => window.escapeHtml(String(value ?? ''));
    container.innerHTML = items.map((item, index) => {
        const meta = item.metadata || {};
        const userIds = [...new Set([
            ...(Array.isArray(meta.associated_user_ids) ? meta.associated_user_ids : []),
            ...(!meta.associated_user_ids?.length ? [meta.user_id] : [])
        ].filter(Boolean).map(String))];
        const notes = [meta.manual_entry ? '人工录入' : '', meta.manually_edited ? '人工修订' : '', meta.topic].filter(Boolean);
        const participant = uid => escape(userIdToName[uid] || String(uid));
        const importance = Number(item.importance || 5);
        const importanceCategory = importance <= 3 ? 'low' : importance >= 8 ? 'high' : 'medium';
        const evidence = Array.isArray(meta.evidence) ? meta.evidence : [];
        const sources = Array.isArray(meta.source_memory_ids) ? meta.source_memory_ids : [];
        const isLong = (item.text || '').length > 120 || ((item.text || '').match(/\n/g) || []).length >= 3;
        const knownStatus = Object.hasOwn(MEMORY_STATUSES, item.status) ? item.status : 'active';
        return `
            <article class="memory-card" data-memory-index="${index}">
                <div class="memory-card-header">
                    <span class="memory-kind-badge">${escape(MEMORY_KINDS[item.kind] || '事件')}</span>
                    <span class="memory-status-badge memory-status-${knownStatus}">${escape(MEMORY_STATUSES[item.status] || item.status || '有效')}</span>
                </div>
                ${notes.length ? `<div class="memory-context">${notes.map(note => `<span>${escape(note)}</span>`).join('')}</div>` : ''}
                <div class="memory-card-body">
                    <div class="memory-text-container collapsed">${escape(item.text || '').replace(/\n/g, '<br>')}</div>
                    ${isLong ? '<button type="button" class="btn-text-toggle" data-memory-action="toggle">展开全部</button>' : ''}
                </div>
                ${meta.observed_from ? `<div class="memory-observation">观察时间：${escape(window.formatDate(meta.observed_from))} — ${escape(window.formatDate(meta.observed_until || meta.observed_from))}</div>` : ''}
                ${item.expires_at ? `<div class="memory-observation">到期时间：${escape(window.formatDate(item.expires_at))}</div>` : ''}
                ${!meta.observed_from && meta.occurred_at ? `<div class="memory-observation">发生时间：${escape(window.formatDate(meta.occurred_at))}</div>` : ''}
                <details class="memory-evidence">
                    <summary>查看依据 · ${evidence.length} 条原话${sources.length ? ` · ${sources.length} 条来源记忆` : ''}</summary>
                    ${meta.manually_edited ? '<p class="muted-text">此记录经过人工修订，以下保留原始依据。</p>' : ''}
                    ${evidence.length ? `<ol>${evidence.map(ref => `
                        <li>
                            <div class="memory-evidence-meta">${escape(ref.user_id || '未知用户')} · ${escape(window.formatDate(ref.time))}</div>
                            <blockquote>${escape(ref.quote || '')}</blockquote>
                            <div class="memory-evidence-meta">消息 ${escape(String(ref.message_id || ref.source_id || '未知'))}${Array.isArray(ref.media_ids) && ref.media_ids.length ? ` · 媒体 ${escape(ref.media_ids.join('、'))}` : ''}</div>
                        </li>`).join('')}</ol>` : `<p class="muted-text">${meta.manual_entry ? '管理员手动录入，无聊天原话证据。' : '此记录没有保存原话证据。'}</p>`}
                    ${sources.length ? `<div class="memory-source-ids">来源记忆：${sources.map(id => `<code>${escape(String(id))}</code>`).join('、')}</div>` : ''}
                </details>
                <div class="memory-metrics">
                    <span class="memory-importance-badge importance-${importanceCategory}"><span class="importance-dot"></span>重要度 ${importance}</span>
                    <span class="memory-activity-badge" title="上次命中: ${escape(item.last_hit_at ? window.formatDate(item.last_hit_at) : '从未命中')}">${Number(item.hit_count || 0)} 次命中</span>
                </div>
                <div class="memory-card-footer">
                    <div class="memory-associated-users">${userIds.map(uid => `<button type="button" class="user-pill" data-memory-user="${escape(uid)}" title="用户 ID: ${escape(uid)}，点击筛选">${participant(uid)}</button>`).join('') || '<span class="muted-text">无关联用户</span>'}</div>
                    <div class="memory-actions">
                        <button type="button" class="btn btn-secondary btn-small" data-memory-action="edit">编辑</button>
                        <button type="button" class="btn btn-danger btn-small" data-memory-action="delete">删除</button>
                    </div>
                </div>
                <div class="memory-time-details"><span>创建于 ${escape(window.formatDate(item.created_at))}</span></div>
            </article>`;
    }).join('');

    // Keep identifiers and memory text out of executable inline attributes.
    container.onclick = event => {
        const button = event.target.closest('button');
        const card = button?.closest('[data-memory-index]');
        if (!card) return;
        const item = items[Number(card.dataset.memoryIndex)];
        if (button.dataset.memoryUser !== undefined) {
            window.filterMemoryByUser(button.dataset.memoryUser);
        } else if (button.dataset.memoryAction === 'edit') {
            window.openEditMemoryModal(item);
        } else if (button.dataset.memoryAction === 'delete') {
            window.deleteMemory(item.memory_id, item.bot_name, item.group_or_user_id);
        } else if (button.dataset.memoryAction === 'toggle') {
            const text = card.querySelector('.memory-text-container');
            const collapsed = text.classList.toggle('collapsed');
            text.classList.toggle('expanded', !collapsed);
            button.textContent = collapsed ? '展开全部' : '收起全部';
        }
    };
}
