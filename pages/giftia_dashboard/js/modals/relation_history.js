import { renderRelationChanges } from '../modules/render/relation_history.js';

let currentProfile = null;
let historyRequestId = 0;
let historyItems = [];
let historyPage = 1;
let historyRevision = null;
let currentEditor = null;
let mutationPending = false;

function renderHistoryScore(item) {
    document.getElementById('user-relation-history-summary').innerHTML = `
        <div class="relation-score-total">
            <span class="relation-score-label">当前好感度</span>
            <strong class="relation-score-value">${Number(item.relation ?? 50).toFixed(1)}</strong>
            <span class="relation-score-formula">基础 ${Number(item.base_relation ?? 0)} + 互动 ${Number(item.interaction_score ?? 50).toFixed(1)}</span>
        </div>
        <dl class="relation-score-windows">
            <div>
                <dt>近期分</dt>
                <dd>${Number(item.recent_score ?? 50).toFixed(1)}<span>最近 ${Number(item.recent_count ?? 0)} 次评价</span></dd>
            </div>
            <div>
                <dt>长期分</dt>
                <dd>${Number(item.long_term_score ?? 50).toFixed(1)}<span>累计 ${Number(item.total_count ?? 0)} 次评价</span></dd>
            </div>
        </dl>
    `;
}

window.openUserRelationHistoryModal = async function(item) {
    currentProfile = {
        bot_name: item.bot_name,
        group_or_user_id: item.group_or_user_id,
        user_id: item.user_id
    };
    const name = item.call_name ? `${item.call_name}（${item.user_id}）` : String(item.user_id);
    document.getElementById('user-relation-history-scope').textContent = `${name} · ${item.bot_name} · 跨群共享（当前会话 ${item.group_or_user_id}）`;
    renderHistoryScore(item);
    window.openModal('user-relation-history-modal');
    await window.loadUserRelationHistory(1);
};

window.loadUserRelationHistory = async function(page = 1) {
    if (!currentProfile) return;
    const profile = currentProfile;
    const requestId = ++historyRequestId;
    historyRevision = null;
    historyItems = [];
    window.closeUserRelationEditor();
    document.getElementById('user-relation-add').disabled = true;
    const list = document.getElementById('user-relation-history-list');
    const pagination = document.getElementById('user-relation-history-pagination');
    list.innerHTML = '<div class="loading-row"><span class="loader"></span> 加载互动评价...</div>';
    pagination.innerHTML = '';
    try {
        const res = await window.apiGet('/profiles/user/relation_history', {
            ...profile, page, limit: 10
        });
        if (requestId !== historyRequestId || profile !== currentProfile) return;
        if (res.status !== 'success' || !res.data) {
            throw new Error(res.message || '请求失败');
        }
        historyItems = res.data.items || [];
        historyPage = res.data.page;
        historyRevision = res.data.revision ?? 0;
        if (res.data.score) renderHistoryScore(res.data.score);
        document.getElementById('user-relation-add').disabled = mutationPending;
        list.innerHTML = renderRelationChanges(historyItems, { editable: true });
        list.onclick = event => {
            const button = event.target.closest('button[data-relation-action]');
            if (!button) return;
            const index = Number(button.closest('[data-relation-index]').dataset.relationIndex);
            if (button.dataset.relationAction === 'edit') window.openUserRelationEditor(index);
            if (button.dataset.relationAction === 'delete') window.deleteUserRelationChange(index);
        };
        document.getElementById('user-relation-history-content').scrollTop = 0;
        const paginationState = { page: res.data.page, limit: 10, total: res.data.total };
        window.renderPagination('user-relation-history-pagination', paginationState,
            nextPage => window.loadUserRelationHistory(nextPage));
    } catch (error) {
        if (requestId !== historyRequestId) return;
        list.innerHTML = `<div class="relation-history-error" role="alert"><p>加载失败：${window.escapeHtml(String(error.message))}</p><button type="button" class="btn btn-secondary btn-small" variant="tonal">重试</button></div>`;
        list.querySelector('button').onclick = () => window.loadUserRelationHistory(page);
    }
};

window.openUserRelationEditor = function(index = null) {
    if (!currentProfile || historyRevision === null || mutationPending) return;
    const item = index === null ? null : historyItems[index];
    if (index !== null && !item) return;
    currentEditor = { profile: currentProfile, revision: historyRevision, item, page: historyPage };
    document.getElementById('user-relation-editor-title').textContent = item ? '编辑互动评价' : '新增互动评价';
    document.getElementById('user-relation-delta').value = item?.delta ?? 1;
    document.getElementById('user-relation-reason').value = item?.reason || '';
    document.getElementById('user-relation-editor-error').hidden = true;
    document.getElementById('user-relation-editor').hidden = false;
    document.getElementById('user-relation-reason').focus?.();
};

window.closeUserRelationEditor = function() {
    currentEditor = null;
    document.getElementById('user-relation-editor').hidden = true;
    document.getElementById('user-relation-editor-error').hidden = true;
};

window.submitUserRelationChange = async function() {
    const editor = currentEditor;
    if (!editor || mutationPending) return;
    const error = document.getElementById('user-relation-editor-error');
    const delta = Number(document.getElementById('user-relation-delta').value);
    const reason = document.getElementById('user-relation-reason').value.trim();
    if (!Number.isInteger(delta) || delta === 0 || Math.abs(delta) > 5 || !reason || reason.length > 120) {
        error.textContent = '评价得分须为 -5～+5 的非零整数，内容须为 1～120 字。';
        error.hidden = false;
        return;
    }
    mutationPending = true;
    document.getElementById('user-relation-save').disabled = true;
    document.getElementById('user-relation-add').disabled = true;
    error.hidden = true;
    try {
        const res = await window.apiPost('/profiles/user/relation_history/save', {
            ...editor.profile,
            action: editor.item ? 'update' : 'add',
            revision: editor.revision,
            ...(editor.item ? { id: editor.item.id } : {}),
            delta, reason
        });
        if (res.status !== 'success') throw new Error(res.message || '保存失败');
        if (editor.profile === currentProfile) {
            window.closeUserRelationEditor();
            await window.loadUserRelationHistory(editor.item ? editor.page : 1);
        }
        await window.GiftiaApp.loadUserProfiles();
        window.showToast('互动评价已保存，好感度已重新计算');
    } catch (failure) {
        if (editor === currentEditor) {
            error.textContent = String(failure.message);
            error.hidden = false;
        } else {
            window.showToast(`保存失败：${failure.message}`);
        }
    } finally {
        mutationPending = false;
        document.getElementById('user-relation-save').disabled = false;
        document.getElementById('user-relation-add').disabled = historyRevision === null;
    }
};

window.deleteUserRelationChange = function(index) {
    const item = historyItems[index];
    if (!currentProfile || !item || mutationPending || historyRevision === null) return;
    const profile = currentProfile;
    const revision = historyRevision;
    const page = historyPage;
    window.showConfirm('删除互动评价', '确定删除这条评价吗？删除后会重新计算好感度。', async () => {
        if (mutationPending) return;
        mutationPending = true;
        document.getElementById('user-relation-add').disabled = true;
        try {
            const res = await window.apiPost('/profiles/user/relation_history/save', {
                ...profile, action: 'delete', id: item.id, revision
            });
            if (res.status !== 'success') throw new Error(res.message || '删除失败');
            if (profile === currentProfile) await window.loadUserRelationHistory(page);
            await window.GiftiaApp.loadUserProfiles();
            window.showToast('互动评价已删除，好感度已重新计算');
        } catch (error) {
            window.showToast(`删除失败：${error.message}`);
        } finally {
            mutationPending = false;
            document.getElementById('user-relation-add').disabled = historyRevision === null;
        }
    });
};
