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
await import('../pages/giftia_dashboard/js/modals/memory.js');
await import('../pages/giftia_dashboard/js/modals/profile.js');
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

test('memory cards show facts and escape evidence and account IDs', () => {
    renderMemories([{
        memory_id: "id');throw Error('injection')", kind: 'habit', status: 'completed',
        text: '<img src=x onerror=alert(1)>',
        metadata: {
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
    get('edit-user-prof-relation').value = '5';
    let body;
    window.apiPost = async (path, value) => { body = value; return { status: 'success' }; };
    await window.submitEditUserProfile();
    assert.equal(body.call_name, 'local name');
    assert.equal(body.attitude, 'friendly');
    for (const retired of ['personality', 'interests', 'agreements', 'extra', 'title']) assert.equal(Object.hasOwn(body, retired), false);
    assert.equal(profile.agreements, 'retired agreement');
});
