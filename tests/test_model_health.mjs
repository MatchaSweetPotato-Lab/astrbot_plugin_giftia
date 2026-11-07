import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const fields = new Map();
globalThis.document = {
    addEventListener() {},
    getElementById(id) {
        if (!fields.has(id)) fields.set(id, {innerHTML: '', textContent: '', disabled: false});
        return fields.get(id);
    }
};
globalThis.window = {showToast() {}};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
await import('../pages/giftia_dashboard/js/utils.js');
const { state } = await import('../pages/giftia_dashboard/js/modules/state.js');
const { ALL_TABS, DEFAULT_PINNED_IDS } = await import('../pages/giftia_dashboard/js/modules/nav_manager.js');
const { loadModelHealth, testModelConnection } = await import('../pages/giftia_dashboard/js/modules/model_health.js');
const get = id => document.getElementById(id);
const model = status => ({provider_id: "provider'<>id", name: '<script>name</script>', model: '<img src=x>', registered: true, status, failures: status === 'cooldown' ? 2 : 0, retry_in_seconds: 60, uses: ['bot · 正式回复'], reason: 'server_error'});

beforeEach(() => {
    fields.clear();
    state.activeTab = 'model-health';
    window.showToast = () => {};
    window.apiGet = async () => ({status: 'success', data: [model('cooldown')]});
});

test('model status is discoverable in the standard navigation', () => {
    assert.equal(ALL_TABS.find(tab => tab.id === 'model-health').name, '模型状态');
    assert.ok(DEFAULT_PINNED_IDS.includes('model-health'));
});

test('model status escapes provider names, model IDs and action attributes', async () => {
    await loadModelHealth();
    const html = get('model-health-list').innerHTML;
    assert.match(html, /冷却中/);
    assert.match(html, /60 秒/);
    assert.match(html, /&lt;script&gt;name/);
    assert.doesNotMatch(html, /<script>|<img|data-test-model="provider'<>/);
});

test('duplicate tests are blocked and success refreshes the shared health display', async () => {
    await loadModelHealth();
    let finish;
    let calls = 0;
    window.apiPost = async (endpoint, body) => {
        calls++;
        assert.equal(endpoint, '/models/test');
        assert.equal(body.provider_id, "provider'<>id");
        return await new Promise(resolve => { finish = resolve; });
    };
    const pending = testModelConnection("provider'<>id");
    await testModelConnection("provider'<>id");
    assert.equal(calls, 1);
    assert.match(get('model-health-list').innerHTML, /disabled/);
    window.apiGet = async () => ({status: 'success', data: [model('healthy')]});
    finish({status: 'success', message: '恢复成功'});
    await pending;
    assert.match(get('model-health-list').innerHTML, /健康/);
    assert.doesNotMatch(get('model-health-list').innerHTML, /冷却中|disabled/);
});

test('a stale list response cannot overwrite a newer health snapshot', async () => {
    let finish;
    window.apiGet = () => new Promise(resolve => { finish = resolve; });
    const pending = loadModelHealth();
    window.apiGet = async () => ({status: 'success', data: [model('healthy')]});
    await loadModelHealth();
    finish({status: 'success', data: [model('cooldown')]});
    await pending;
    assert.match(get('model-health-list').innerHTML, /健康/);
    assert.doesNotMatch(get('model-health-list').innerHTML, /冷却中/);
});

test('refresh errors preserve the last known rows and re-enable refresh', async () => {
    await loadModelHealth();
    const before = get('model-health-list').innerHTML;
    window.apiGet = async () => { throw new Error('offline'); };
    await loadModelHealth();
    assert.equal(get('model-health-list').innerHTML, before);
    assert.equal(get('model-health-error').textContent, 'offline');
    assert.equal(get('model-health-refresh').disabled, false);
});
