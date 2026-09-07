import { populateBotSelect, populateSessionSelect } from '../filters.js';

const pagination = { page: 1, limit: 15, total: 0 };
let requestId = 0;
let filterRequestId = 0;

export async function initializeSlangTab(preserveSession = true) {
    const current = ++filterRequestId;
    const bot = document.getElementById('slang-bot');
    const sessionSelect = document.getElementById('slang-session');
    const prevSession = sessionSelect ? sessionSelect.value : '';
    try {
        const res = await window.apiGet('/slang/filter_options', { bot_name: bot.value });
        if (current !== filterRequestId) return;
        if (res.status !== 'success') throw new Error(res.message || '获取筛选项失败');
        populateBotSelect(bot, res.data.bots, res.data.selected_bot_name);

        const sessions = res.data.sessions || [];
        if (sessionSelect) {
            const nextSession = (preserveSession && prevSession && sessions.some(item => item.group_or_user_id === prevSession))
                ? prevSession
                : (sessions[0] ? sessions[0].group_or_user_id : '');
            populateSessionSelect(sessionSelect, sessions, nextSession);
        }

        const datalist = document.getElementById('slang-sessions');
        if (datalist) {
            datalist.replaceChildren(
                ...sessions.map(s => new Option(s.group_or_user_id, s.group_or_user_id))
            );
        }
        document.getElementById('slang-add').disabled = !bot.value;
        await loadSlang();
    } catch (error) {
        if (current === filterRequestId) window.showToast(error.message);
    }
}

export function resetSlangPagination() {
    pagination.page = 1;
}

export async function loadSlang() {
    const current = ++requestId;
    const list = document.getElementById('slang-list');
    const bot = document.getElementById('slang-bot').value;
    const session = document.getElementById('slang-session')?.value.trim() || '';
    list.innerHTML = '<div class="loading-row"><span class="loader"></span>加载中...</div>';
    try {
        const res = (bot && session) ? await window.apiGet('/slang', {
            bot_name: bot,
            group_or_user_id: session,
            search: document.getElementById('slang-search').value.trim(),
            page: pagination.page,
            limit: pagination.limit,
        }) : { status: 'success', data: { items: [], total: 0 } };
        if (current !== requestId) return;
        if (res.status !== 'success') throw new Error(res.message || '获取黑话失败');
        pagination.total = res.data.total;
        const lastPage = Math.max(1, Math.ceil(pagination.total / pagination.limit));
        if (pagination.page > lastPage) {
            pagination.page = lastPage;
            return loadSlang();
        }
        list.replaceChildren();
        for (const entry of res.data.items) {
            const card = document.createElement('div');
            card.className = 'slang-card';

            const header = document.createElement('div');
            header.className = 'slang-card-header';

            const badge = document.createElement('div');
            badge.className = 'slang-term-badge';
            const termTitle = document.createElement('span');
            termTitle.className = 'slang-term-title';
            termTitle.textContent = entry.term;
            badge.append(termTitle);

            const actions = document.createElement('div');
            actions.className = 'slang-card-actions';

            const edit = document.createElement('button');
            edit.type = 'button';
            edit.className = 'btn-icon-action';
            edit.title = '编辑';
            edit.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>';
            edit.addEventListener('click', () => openSlangModal(entry));

            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'btn-icon-action danger';
            remove.title = '删除';
            remove.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>';
            remove.addEventListener('click', () => window.showConfirm(
                '删除黑话', `确定删除黑话「${entry.term}」？`, async () => {
                remove.disabled = true;
                try {
                    const result = await window.apiPost('/slang/delete', {
                        bot_name: entry.bot_name,
                        group_or_user_id: entry.group_or_user_id,
                        term: entry.term,
                    });
                    if (result.status !== 'success') throw new Error(result.message || '删除失败');
                    window.showToast('黑话已删除');
                    await initializeSlangTab(true);
                } catch (error) {
                    window.showToast(error.message);
                } finally {
                    remove.disabled = false;
                }
            }));

            actions.append(edit, remove);
            header.append(badge, actions);

            const cardBody = document.createElement('div');
            cardBody.className = 'slang-card-body';
            const desc = document.createElement('div');
            desc.className = 'slang-desc-text';
            desc.textContent = entry.description;
            cardBody.append(desc);

            const footer = document.createElement('div');
            footer.className = 'slang-card-footer';
            const timeSpan = document.createElement('span');
            const formattedTime = entry.updated_at ? entry.updated_at.replace('T', ' ').split('.')[0] : '-';
            timeSpan.textContent = `更新于 ${formattedTime}`;
            footer.append(timeSpan);

            card.append(header, cardBody, footer);
            list.append(card);
        }
        if (!res.data.items.length) {
            list.innerHTML = '<div class="no-data-row">暂无黑话，可点击“新增黑话”添加当前会话的词汇解释。</div>';
        }
        window.renderPagination('slang-pagination', pagination, page => {
            pagination.page = page;
            loadSlang();
        });
    } catch (error) {
        if (current !== requestId) return;
        list.innerHTML = `<div class="no-data-row">${window.escapeHtml(error.message || '获取黑话失败')}</div>`;
        document.getElementById('slang-pagination').replaceChildren();
    }
}

export function openSlangModal(entry = null) {
    const bot = entry?.bot_name || document.getElementById('slang-bot').value;
    if (!bot) return window.showToast('请先选择机器人');
    document.getElementById('slang-form').reset();
    document.getElementById('slang-modal-title').textContent = entry ? '编辑黑话' : '新增黑话';
    document.getElementById('slang-edit-bot').value = bot;
    const session = document.getElementById('slang-edit-session');
    session.value = entry?.group_or_user_id || document.getElementById('slang-session').value.trim();
    session.readOnly = Boolean(entry);
    const term = document.getElementById('slang-edit-term');
    term.value = entry?.term || '';
    term.readOnly = Boolean(entry);
    document.getElementById('slang-edit-description').value = entry?.description || '';
    document.getElementById('slang-form-error').textContent = '';
    window.openModal('slang-modal');
    (entry ? document.getElementById('slang-edit-description') : session.value ? term : session).focus();
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('slang-add')?.addEventListener('click', () => openSlangModal());
    document.getElementById('slang-form')?.addEventListener('submit', async event => {
        event.preventDefault();
        const save = document.getElementById('slang-save');
        if (save.disabled) return;
        save.disabled = true;
        document.getElementById('slang-form-error').textContent = '';
        try {
            const savedSession = document.getElementById('slang-edit-session').value.trim();
            const res = await window.apiPost('/slang/save', {
                bot_name: document.getElementById('slang-edit-bot').value,
                group_or_user_id: savedSession,
                term: document.getElementById('slang-edit-term').value.trim(),
                description: document.getElementById('slang-edit-description').value.trim(),
            });
            if (res.status !== 'success') throw new Error(res.message || '保存失败');
            window.closeModal('slang-modal');
            window.showToast('黑话已保存');
            pagination.page = 1;
            const sessionSelect = document.getElementById('slang-session');
            if (sessionSelect && savedSession) {
                sessionSelect.value = savedSession;
            }
            await initializeSlangTab(true);
        } catch (error) {
            document.getElementById('slang-form-error').textContent = error.message;
        } finally {
            save.disabled = false;
        }
    });
});
