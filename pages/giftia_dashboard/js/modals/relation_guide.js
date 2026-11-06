// Bot-scoped affinity guidance editor.
let defaultGuide = '';
let defaultDeltaGuide = '';
let defaultTierCounts = [];
let activeGuideTab = 'score';
let editorVersion = 0;

window.switchRelationGuideTab = function(tab, event) {
    const tabs = ['score', 'delta', 'scoring'];
    if (event) {
        const index = tabs.indexOf(tab);
        const next = { ArrowRight: (index + 1) % 3, ArrowLeft: (index + 2) % 3, Home: 0, End: 2 }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        tab = tabs[next];
        document.getElementById(`relation-guide-tab-${tab}`).focus();
    }
    if (!tabs.includes(tab)) return;
    activeGuideTab = tab;
    for (const name of tabs) {
        const selected = name === tab;
        const button = document.getElementById(`relation-guide-tab-${name}`);
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', String(selected));
        button.tabIndex = selected ? 0 : -1;
        document.getElementById(`relation-guide-panel-${name}`).hidden = !selected;
    }
};

window.openRelationScoreGuideModal = async function() {
    const bot = document.getElementById('profile-bot-name').value;
    if (!bot) {
        window.showToast('请先选择 Bot');
        return;
    }
    const button = document.getElementById('relation-score-guide-btn');
    if (button.disabled) return;
    button.disabled = true;
    try {
        const res = await window.apiGet('/profiles/user/relation_guide', { bot_name: bot });
        if (res.status !== 'success' || !res.data) throw new Error(res.message || '读取失败');
        editorVersion++;
        defaultGuide = res.data.default_guide;
        defaultDeltaGuide = res.data.default_delta_guide;
        defaultTierCounts = res.data.default_tier_counts;
        document.getElementById('relation-score-guide-bot').value = bot;
        document.getElementById('relation-score-guide-scope').textContent = `Bot：${bot} · 所有群聊与私聊共用`;
        document.getElementById('relation-score-guide-text').value = res.data.guide;
        document.getElementById('relation-delta-guide-text').value = res.data.delta_guide;
        res.data.tier_counts.forEach((count, index) => {
            document.getElementById(`relation-tier-count-${index + 1}`).value = count;
        });
        window.switchRelationGuideTab('score');
        document.getElementById('relation-score-guide-save').disabled = false;
        window.openModal('relation-score-guide-modal');
        document.getElementById('relation-score-guide-content').scrollTop = 0;
        document.getElementById('relation-score-guide-text').scrollTop = 0;
    } catch (e) {
        window.showToast(`读取好感度说明失败：${e.message}`);
    } finally {
        button.disabled = false;
    }
};

window.restoreDefaultRelationScoreGuide = function() {
    if (activeGuideTab === 'score') {
        document.getElementById('relation-score-guide-text').value = defaultGuide;
    } else if (activeGuideTab === 'delta') {
        document.getElementById('relation-delta-guide-text').value = defaultDeltaGuide;
    } else {
        defaultTierCounts.forEach((count, index) => {
            document.getElementById(`relation-tier-count-${index + 1}`).value = count;
        });
    }
    window.showToast('已恢复当前页默认内容，保存后生效');
};

window.saveRelationScoreGuide = async function() {
    const button = document.getElementById('relation-score-guide-save');
    if (button.disabled) return;
    const bot = document.getElementById('relation-score-guide-bot').value;
    const version = editorVersion;
    const guide = document.getElementById('relation-score-guide-text').value.trim();
    const deltaGuide = document.getElementById('relation-delta-guide-text').value.trim();
    const tierCounts = [1, 2, 3, 4, 5].map(tier => Number(document.getElementById(`relation-tier-count-${tier}`).value));
    if (!bot || guide.length > 12000 || deltaGuide.length > 12000) {
        window.showToast('请选择 Bot，每份说明最多 12000 字');
        return;
    }
    if (tierCounts.some(count => !Number.isInteger(count) || count < 1 || count > 1000000)) {
        window.switchRelationGuideTab('scoring');
        window.showToast('达档次数必须为 1～1000000 的整数');
        return;
    }
    button.disabled = true;
    try {
        const res = await window.apiPost('/profiles/user/relation_guide', {
            bot_name: bot, guide, delta_guide: deltaGuide, tier_counts: tierCounts
        });
        if (res.status !== 'success') throw new Error(res.message || '保存失败');
        window.showToast('好感度说明已保存');
        if (version === editorVersion) {
            window.closeModal('relation-score-guide-modal');
            if (document.getElementById('profile-bot-name').value === bot) {
                await window.GiftiaApp?.loadUserProfiles?.();
            }
        }
    } catch (e) {
        window.showToast(`保存好感度说明失败：${e.message}`);
    } finally {
        if (version === editorVersion) button.disabled = false;
    }
};
