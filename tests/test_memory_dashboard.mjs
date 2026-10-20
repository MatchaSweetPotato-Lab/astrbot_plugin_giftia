import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const fields = new Map();
class Field {
    value = '';
    innerHTML = '';
    textContent = '';
    disabled = false;
    hidden = false;
    options = [];
    style = {};
    children = new Map();
    querySelector(selector) {
        if (!this.children.has(selector)) this.children.set(selector, new Field());
        return this.children.get(selector);
    }
    replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; }
    append(...options) { this.options.push(...options); }
}
globalThis.Option = class {
    constructor(text, value) { this.text = text; this.value = value; }
};
globalThis.document = {
    addEventListener() {},
    getElementById(id) {
        if (!fields.has(id)) fields.set(id, new Field());
        return fields.get(id);
    }
};
globalThis.window = {};
await import('../pages/giftia_dashboard/js/utils.js');
const { state } = await import('../pages/giftia_dashboard/js/modules/state.js');
const { loadMemories, renderMemories } = await import('../pages/giftia_dashboard/js/modules/render/memories.js');
const { renderChatHistory } = await import('../pages/giftia_dashboard/js/modules/render/history.js');
const { renderRelationChanges } = await import('../pages/giftia_dashboard/js/modules/render/relation_history.js');
const { renderUserProfiles } = await import('../pages/giftia_dashboard/js/modules/render/profiles.js');
await import('../pages/giftia_dashboard/js/modals/memory.js');
await import('../pages/giftia_dashboard/js/modals/profile.js');
await import('../pages/giftia_dashboard/js/modals/relation_history.js');
const get = id => document.getElementById(id);

beforeEach(() => {
    fields.clear();
    state.pagination.memories = { page: 1, limit: 15, total: 0 };
    for (const [id, value] of Object.entries({
        'memory-bot-name': 'bot', 'memory-group-id': 'group'
    })) get(id).value = value;
    window.renderPagination = () => {};
    window.openModal = () => {};
    window.closeModal = () => {};
    window.showToast = () => {};
    window.GiftiaApp = {
        getScopedFilterParams: () => ({
            bot_name: get('memory-bot-name').value
        }),
        resetPagination: () => {},
        refreshScopedFilters: async () => {},
        loadMemories: async () => {},
        loadUserProfiles: async () => {}
    };
});

test('history badges use each message observation state independently', () => {
    renderChatHistory([
        { id: 1, memory_status: 'unseen', content: 'unseen', created_at: '2026-10-01' },
        { id: 2, memory_status: 'pending', content: 'pending', created_at: '2026-10-02' },
        { id: 3, memory_status: 'summarized', content: 'done', created_at: '2026-10-03' },
    ]);
    const html = get('history-list').innerHTML;
    assert.match(html, /未见/);
    assert.match(html, /已见待总结/);
    assert.match(html, /已总结/);
    assert.doesNotMatch(html, /已归档|待总结\/跳过/);
});

test('memory cards show facts and escape evidence and account IDs', () => {
    renderMemories([{
        memory_id: "id');throw Error('injection')", kind: 'habit', status: 'completed',
        text: '<img src=x onerror=alert(1)>',
        metadata: {
            topic: '<script>topic attack</script>',
            status: 'active', associated_user_ids: ["A' onclick='alert(1)", 'B', 'C'],
            evidence: [{ source_id: 1, quote: '<script>attack</script>', user_id: 'A' }],
            observed_from: '2026-10-01', observed_until: '2026-10-12'
        }
    }]);
    const html = get('memory-list').innerHTML;
    assert.doesNotMatch(html, /已完成|记忆类型|memory-status|memory-kind/);
    assert.doesNotMatch(html, /主体|对象|整理归纳|本人自述/);
    assert.match(html, /观察时间/);
    assert.match(html, /&lt;script&gt;attack/);
    assert.match(html, /&lt;script&gt;topic attack/);
    assert.match(html, /&lt;img/);
    assert.doesNotMatch(html, /<script>|<img|onclick=["']|throw Error/);
});

test('edits pass the exact atom object without executable inline arguments', () => {
    const item = { kind: 'event', status: 'active', text: "it's okay", metadata: {} };
    const original = window.openEditMemoryModal;
    let opened;
    window.openEditMemoryModal = value => { opened = value; };
    try {
        renderMemories([item]);
        const card = { dataset: { memoryIndex: '0' } };
        const button = { dataset: { memoryAction: 'edit' }, closest: () => card };
        get('memory-list').onclick({ target: { closest: () => button } });
        assert.equal(opened, item);
    } finally {
        window.openEditMemoryModal = original;
    }
});

test('evidence displays all attachments from one source message', () => {
    renderMemories([{
        kind: 'event', status: 'active', text: 'message evidence',
        metadata: {
            evidence: [
                { source_id: 1, quote: 'whole message', media_ids: ['image-a', '<image-b>'] }
            ]
        }
    }]);
    const html = get('memory-list').innerHTML;
    assert.match(html, /媒体 image-a、&lt;image-b&gt;/);
    assert.doesNotMatch(html, /<image-b>/);
});

test('slower responses cannot replace the newly selected conversation', async () => {
    const pending = [];
    window.apiGet = () => new Promise(resolve => pending.push(resolve));
    const old = loadMemories();
    get('memory-group-id').value = 'new-group';
    const recent = loadMemories();
    pending[1]({ status: 'success', data: { total: 1, items: [{ text: 'new conversation', metadata: {} }] } });
    await recent;
    pending[0]({ status: 'success', data: { total: 99, items: [{ text: 'old conversation', metadata: {} }] } });
    await old;
    assert.match(get('memory-list').innerHTML, /new conversation/);
    assert.doesNotMatch(get('memory-list').innerHTML, /old conversation/);
    assert.equal(state.pagination.memories.total, 1);
});

test('fact edits preserve revision and source evidence without classification fields', async () => {
    const item = {
        memory_id: 'fact', bot_name: 'bot', group_or_user_id: 'group', revision: 7,
        text: 'original text', metadata: { topic: '角色喜好', evidence: [{ quote: 'original' }] }
    };
    window.openEditMemoryModal(item);
    let body;
    window.apiPost = async (path, value) => { body = value; return { status: 'success' }; };
    get('edit-mem-text').value = 'corrected text';
    await window.submitEditMemory();
    assert.equal(body.revision, 7);
    assert.equal(body.text, 'corrected text');
    for (const immutable of ['status', 'expires_at', 'evidence', 'user_id', 'kind', 'subject_id']) {
        assert.equal(Object.hasOwn(body, immutable), false);
    }
    assert.deepEqual(item.metadata.evidence, [{ quote: 'original' }]);
});

test('manual creation saves all participants and selects the saved conversation', async () => {
    window.openAddMemoryModal();
    const values = { bot: 'new-bot', group: 'new-group', 'associated-ids': 'A,B,C', topic: 'emoji', text: 'A often teases B' };
    for (const [field, value] of Object.entries(values)) get(`add-mem-${field}`).value = value;
    let saved;
    window.apiPost = async (path, body) => { saved = body; return { status: 'success' }; };
    await window.submitAddMemory();
    assert.equal(saved.associated_user_ids, 'A,B,C');
    for (const field of ['kind', 'status', 'expires_at', 'subject_id', 'object_id', 'origin']) assert.equal(Object.hasOwn(saved, field), false);
    assert.equal(get('memory-bot-name').value, 'new-bot');
    assert.equal(get('memory-group-id').value, 'new-group');
});

test('profile rendering and saves exclude retired fields without clearing stored history', async () => {
    const profile = {
        bot_name: 'bot', group_or_user_id: 'group', user_id: 'A', relation: 5,
        call_name: 'local name', attitude: 'friendly', avatar_description: 'cat',
        personality: 'retired personality', interests: 'retired interests',
        agreements: 'retired agreement', extra: 'retired extra', title: 'retired title'
    };
    const html = window.renderProfileCard(profile);
    assert.doesNotMatch(html, /retired/);
    assert.match(html, /local name/);
    window.openEditUserProfileModal(profile);
    get('edit-user-prof-base-relation').value = '5';
    let body;
    window.apiPost = async (path, value) => { body = value; return { status: 'success' }; };
    await window.submitEditUserProfile();
    assert.equal(body.call_name, 'local name');
    assert.equal(body.attitude, 'friendly');
    assert.equal(body.base_relation, 5);
    assert.equal(Object.hasOwn(body, 'relation'), false);
    for (const retired of ['personality', 'interests', 'agreements', 'extra', 'title']) assert.equal(Object.hasOwn(body, retired), false);
    assert.equal(profile.agreements, 'retired agreement');
});

test('profile cards show three newest changes and open history with the exact scoped object', () => {
    const item = {
        bot_name: "bot'quoted", group_or_user_id: 'group', user_id: '<user>', relation: -1,
        recent_relation_changes: [
            { delta: 2, reason: '<script>reason</script>', occurred_at: '2026-10-18T12:00:00+08:00' },
            { delta: -2, reason: 'negative reason' },
            { delta: 1, reason: 'third reason' },
            { delta: 1, reason: 'fourth must be omitted' }
        ]
    };
    renderUserProfiles([item]);
    const html = get('user-profile-list').innerHTML;
    assert.equal((html.match(/class="relation-change-item"/g) || []).length, 3);
    assert.match(html, /&lt;script&gt;reason/);
    assert.match(html, /&lt;user&gt;/);
    assert.match(html, /互动评价 \+2/);
    assert.doesNotMatch(html, /→|relation-change-scores|好感度从/);
    assert.match(html, /profile-relation-badge/);
    assert.doesNotMatch(html, /fourth must be omitted|<script>|onclick=/);
    let opened;
    const original = window.openUserRelationHistoryModal;
    window.openUserRelationHistoryModal = value => { opened = value; };
    try {
        const card = { dataset: { profileIndex: '0' } };
        const button = { dataset: { profileAction: 'relation-history' }, closest: () => card };
        get('user-profile-list').onclick({ target: { closest: () => button } });
        assert.equal(opened, item);
    } finally {
        window.openUserRelationHistoryModal = original;
    }
});

test('profile without changes still offers the zero affinity history button', () => {
    for (const changes of [undefined, []]) {
        renderUserProfiles([{ user_id: 'new', bot_name: 'bot', group_or_user_id: 'group', relation: 0, recent_relation_changes: changes }]);
        const html = get('user-profile-list').innerHTML;
        assert.doesNotMatch(html, /最近互动评价|暂无互动评价记录|profile-relation-history/);
        assert.match(html, /data-profile-action="relation-history"/);
        assert.match(html, /0\.0 好感度/);
    }
});

test('profile scores retain fractions and show components and counts in the affinity modal', async () => {
    const profile = {
        user_id: 'friend', relation: 83.1818, base_relation: 0,
        interaction_score: 83.1818, recent_score: 75, long_term_score: 95.4545,
        recent_count: 10, total_count: 100
    };
    renderUserProfiles([profile]);
    const html = get('user-profile-list').innerHTML;
    assert.match(html, /83\.2 好感度/);
    assert.doesNotMatch(html, /基础|互动 83\.2|近期|长期|100 次评价/);
    window.apiGet = async () => ({ status: 'success', data: { items: [], total: 100, page: 1 } });
    await window.openUserRelationHistoryModal(profile);
    const summary = get('user-relation-history-summary').innerHTML;
    assert.match(summary, /83\.2/);
    assert.match(summary, /基础 0 \+ 互动 83\.2/);
    assert.match(summary, /75\.0/);
    assert.match(summary, /95\.5/);
    assert.match(summary, /最近 10 次评价/);
    assert.match(summary, /累计 100 次评价/);
});

test('editing a fractional base does not submit an update', async () => {
    window.openEditUserProfileModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    get('edit-user-prof-base-relation').value = '1.5';
    let posted = false;
    let message;
    window.apiPost = async () => { posted = true; return { status: 'success' }; };
    window.showToast = value => { message = value; };
    await window.submitEditUserProfile();
    assert.equal(posted, false);
    assert.match(message, /基础分必须为有效整数/);
});

test('affinity history opens on page one and paginates ten scoped records', async () => {
    const calls = [];
    let paginate;
    window.apiGet = async (path, params) => {
        calls.push({ path, params });
        return { status: 'success', data: {
            items: [{ delta: 1, reason: `page ${params.page}` }],
            total: 23, page: params.page, limit: 10
        } };
    };
    window.renderPagination = (id, pagination, onChange) => {
        assert.equal(id, 'user-relation-history-pagination');
        assert.equal(pagination.limit, 10);
        assert.equal(pagination.total, 23);
        paginate = onChange;
    };
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    assert.match(get('user-relation-history-scope').textContent, /A · bot · 跨群共享（当前会话 group）/);
    assert.match(get('user-relation-history-list').innerHTML, /page 1/);
    await paginate(2);
    assert.deepEqual(calls[1], { path: '/profiles/user/relation_history', params: {
        bot_name: 'bot', group_or_user_id: 'group', user_id: 'A', page: 2, limit: 10
    } });
    assert.match(get('user-relation-history-list').innerHTML, /page 2/);
});

test('slower affinity history responses cannot replace the newly selected member', async () => {
    const pending = [];
    window.apiGet = () => new Promise(resolve => pending.push(resolve));
    const old = window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'old', user_id: 'A' });
    const recent = window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'new', user_id: 'B' });
    pending[1]({ status: 'success', data: { items: [{ delta: 1, reason: 'new member' }], total: 1, page: 1, limit: 10 } });
    await recent;
    pending[0]({ status: 'success', data: { items: [{ delta: 1, reason: 'old member' }], total: 99, page: 1, limit: 10 } });
    await old;
    assert.match(get('user-relation-history-list').innerHTML, /new member/);
    assert.doesNotMatch(get('user-relation-history-list').innerHTML, /old member/);
    assert.match(get('user-relation-history-scope').textContent, /B · bot · 跨群共享（当前会话 new）/);
});

test('affinity history escapes failures and offers retry and empty states', async () => {
    window.apiGet = async () => ({ status: 'error', message: '<script>failure</script>' });
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    const list = get('user-relation-history-list');
    assert.match(list.innerHTML, /&lt;script&gt;failure/);
    assert.doesNotMatch(list.innerHTML, /<script>/);
    assert.equal(get('user-relation-history-pagination').innerHTML, '');
    window.apiGet = async () => ({ status: 'success', data: { items: [], total: 0, page: 1, limit: 10 } });
    await list.querySelector('button').onclick();
    assert.match(list.innerHTML, /暂无互动评价记录/);
});

test('editing a rating submits scoped content, score and the loaded revision', async () => {
    let revision = 7;
    let rating = { id: 42, delta: 1, reason: '<script>original</script>' };
    let affinity = 54.5;
    let refreshed = 0;
    window.apiGet = async () => ({ status: 'success', data: {
        items: [rating], revision, page: 1, total: 1, limit: 10,
        score: { relation: affinity, interaction_score: affinity, total_count: 1 }
    } });
    window.GiftiaApp.loadUserProfiles = async () => { refreshed++; };
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    const html = get('user-relation-history-list').innerHTML;
    assert.match(html, /data-relation-action="edit"/);
    assert.match(html, /data-relation-action="delete"/);
    assert.match(html, /&lt;script&gt;original/);
    assert.doesNotMatch(html, /<script>|onclick=/);
    window.openUserRelationEditor(0);
    assert.equal(get('user-relation-reason').value, '<script>original</script>');
    get('user-relation-delta').value = '-3';
    get('user-relation-reason').value = 'corrected interaction';
    let submitted;
    window.apiPost = async (path, body) => {
        submitted = { path, body };
        rating = { ...rating, delta: body.delta, reason: body.reason };
        revision++;
        affinity = 38.5;
        return { status: 'success' };
    };
    await window.submitUserRelationChange();
    assert.deepEqual(submitted, { path: '/profiles/user/relation_history/save', body: {
        bot_name: 'bot', group_or_user_id: 'group', user_id: 'A', action: 'update',
        revision: 7, id: 42, delta: -3, reason: 'corrected interaction'
    } });
    assert.equal(get('user-relation-editor').hidden, true);
    assert.match(get('user-relation-history-list').innerHTML, /corrected interaction/);
    assert.match(get('user-relation-history-summary').innerHTML, /38\.5/);
    assert.equal(refreshed, 1);
});

test('adding a rating to empty history creates one entry and blocks duplicate submits', async () => {
    window.apiGet = async () => ({ status: 'success', data: { items: [], revision: 0, total: 0, page: 1, limit: 10 } });
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    assert.equal(get('user-relation-add').disabled, false);
    window.openUserRelationEditor();
    get('user-relation-delta').value = '2';
    get('user-relation-reason').value = 'new positive interaction';
    let finish;
    const submitted = [];
    window.apiPost = (path, body) => {
        submitted.push(body);
        return new Promise(resolve => { finish = resolve; });
    };
    const pending = window.submitUserRelationChange();
    await window.submitUserRelationChange();
    assert.equal(submitted.length, 1);
    assert.deepEqual(submitted[0], { bot_name: 'bot', group_or_user_id: 'group', user_id: 'A',
        action: 'add', revision: 0, delta: 2, reason: 'new positive interaction' });
    assert.equal(get('user-relation-save').disabled, true);
    finish({ status: 'success' });
    await pending;
    assert.equal(get('user-relation-save').disabled, false);
});

test('invalid ratings remain in the editor without making a request', async () => {
    window.apiGet = async () => ({ status: 'success', data: { items: [], revision: 0, total: 0, page: 1, limit: 10 } });
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    let submitted = false;
    window.apiPost = async () => { submitted = true; };
    for (const [delta, reason] of [[0, 'reason'], [6, 'reason'], [1.5, 'reason'], [1, '  ']]) {
        window.openUserRelationEditor();
        get('user-relation-delta').value = String(delta);
        get('user-relation-reason').value = reason;
        await window.submitUserRelationChange();
        assert.equal(get('user-relation-editor-error').hidden, false);
        assert.equal(get('user-relation-editor').hidden, false);
    }
    assert.equal(submitted, false);
});

test('deleting the final entry on a page confirms and reloads the remaining last page', async () => {
    let deleted = false;
    let confirmed;
    let submitted;
    let loadedPage;
    window.apiGet = async (path, params) => {
        loadedPage = params.page;
        return { status: 'success', data: { items: deleted ? [] : [{ id: 11, delta: -1, reason: 'old reason' }],
            revision: deleted ? 5 : 4, total: deleted ? 10 : 11, page: deleted ? 1 : params.page, limit: 10 } };
    };
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    await window.loadUserRelationHistory(2);
    window.showConfirm = (title, message, callback) => { confirmed = callback; };
    window.apiPost = async (path, body) => { submitted = body; deleted = true; return { status: 'success' }; };
    window.deleteUserRelationChange(0);
    assert.equal(deleted, false);
    await confirmed();
    assert.deepEqual(submitted, { bot_name: 'bot', group_or_user_id: 'group', user_id: 'A', action: 'delete', id: 11, revision: 4 });
    assert.equal(loadedPage, 2);
    assert.match(get('user-relation-history-list').innerHTML, /暂无互动评价记录/);
});

test('failed saves preserve unsaved content and display an inline error', async () => {
    window.apiGet = async () => ({ status: 'success', data: { items: [{ id: 1, delta: 1, reason: 'original' }], revision: 0, total: 1, page: 1, limit: 10 } });
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    window.openUserRelationEditor(0);
    get('user-relation-reason').value = 'unsaved revision';
    window.apiPost = async () => ({ status: 'error', message: '历史已更新，请刷新后重试' });
    await window.submitUserRelationChange();
    assert.equal(get('user-relation-reason').value, 'unsaved revision');
    assert.equal(get('user-relation-editor').hidden, false);
    assert.match(get('user-relation-editor-error').textContent, /历史已更新/);
    assert.equal(get('user-relation-save').disabled, false);
});

test('a late save cannot replace the history of a newly selected member', async () => {
    window.apiGet = async (path, params) => ({ status: 'success', data: {
        items: [{ id: 1, delta: 1, reason: `member ${params.user_id}` }], revision: 0, total: 1, page: 1, limit: 10
    } });
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'group', user_id: 'A' });
    window.openUserRelationEditor(0);
    let finish;
    window.apiPost = () => new Promise(resolve => { finish = resolve; });
    const pending = window.submitUserRelationChange();
    await window.openUserRelationHistoryModal({ bot_name: 'bot', group_or_user_id: 'other', user_id: 'B' });
    finish({ status: 'success' });
    await pending;
    assert.match(get('user-relation-history-scope').textContent, /B · bot · 跨群共享（当前会话 other）/);
    assert.match(get('user-relation-history-list').innerHTML, /member B/);
    assert.doesNotMatch(get('user-relation-history-list').innerHTML, /member A/);
});

test('cross-group rating sources are displayed and escaped', () => {
    const html = renderRelationChanges([
        { delta: 2, reason: '本群互动', source_label: '' },
        { delta: -1, reason: '其他群互动', source_label: '来源群聊：<script>source</script>' }
    ]);
    assert.match(html, /来源群聊：&lt;script&gt;source&lt;\/script&gt;/);
    assert.doesNotMatch(html, /<script>/);
    assert.equal((html.match(/来源群聊/g) || []).length, 1);
});
