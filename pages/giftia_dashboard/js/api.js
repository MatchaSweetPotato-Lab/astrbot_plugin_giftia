// Giftia Dashboard API Client

const API_ENVELOPE_STATUSES = new Set(["success", "error", "warning"]);

window.apiGet = async function(endpoint, params) {
    if (window.AstrBotPluginPage) {
        try {
            const res = await window.AstrBotPluginPage.apiGet(endpoint, params);
            if (res && typeof res === "object" && API_ENVELOPE_STATUSES.has(res.status)) {
                return res;
            }
            return { status: "success", data: res };
        } catch (e) {
            return { status: "error", message: e.message };
        }
    }
    return fetch(`${endpoint}?${new URLSearchParams(params)}`).then(r => r.json());
};

window.apiPost = async function(endpoint, body) {
    if (window.AstrBotPluginPage) {
        try {
            const res = await window.AstrBotPluginPage.apiPost(endpoint, body);
            if (res && typeof res === "object" && API_ENVELOPE_STATUSES.has(res.status)) {
                return res;
            }
            return { status: "success", data: res };
        } catch (e) {
            return { status: "error", message: e.message };
        }
    }
    return fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    }).then(r => r.json());
};
