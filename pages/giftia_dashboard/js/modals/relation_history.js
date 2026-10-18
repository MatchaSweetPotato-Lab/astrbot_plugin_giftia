import { renderRelationChanges } from '../modules/render/relation_history.js';

let currentProfile = null;
let historyRequestId = 0;

window.openUserRelationHistoryModal = async function(item) {
    currentProfile = {
        bot_name: item.bot_name,
        group_or_user_id: item.group_or_user_id,
        user_id: item.user_id
    };
    const name = item.call_name ? `${item.call_name}（${item.user_id}）` : String(item.user_id);
    document.getElementById('user-relation-history-scope').textContent = `${name} · ${item.bot_name} / ${item.group_or_user_id}`;
    window.openModal('user-relation-history-modal');
    await window.loadUserRelationHistory(1);
};

window.loadUserRelationHistory = async function(page = 1) {
    if (!currentProfile) return;
    const requestId = ++historyRequestId;
    const list = document.getElementById('user-relation-history-list');
    const pagination = document.getElementById('user-relation-history-pagination');
    list.innerHTML = '<div class="loading-row"><span class="loader"></span> 加载互动评价...</div>';
    pagination.innerHTML = '';
    try {
        const res = await window.apiGet('/profiles/user/relation_history', {
            ...currentProfile, page, limit: 10
        });
        if (requestId !== historyRequestId) return;
        if (res.status !== 'success' || !res.data) {
            throw new Error(res.message || '请求失败');
        }
        list.innerHTML = renderRelationChanges(res.data.items || []);
        list.scrollTop = 0;
        const paginationState = { page: res.data.page, limit: 10, total: res.data.total };
        window.renderPagination('user-relation-history-pagination', paginationState,
            nextPage => window.loadUserRelationHistory(nextPage));
    } catch (error) {
        if (requestId !== historyRequestId) return;
        list.innerHTML = `<div class="relation-history-error" role="alert"><p>加载失败：${window.escapeHtml(String(error.message))}</p><button type="button" class="btn btn-secondary btn-small" variant="tonal">重试</button></div>`;
        list.querySelector('button').onclick = () => window.loadUserRelationHistory(page);
    }
};
