# Conversation model health

Open **模型状态 (Model status)** in Giftia WebUI to see configured providers, model names, usage, health, last request latency and last successful request. Configurations for disabled bots are included and labeled in the usage column. The tab is pinned by default. Use **更多 (More)** when space is limited, or customize navigation to move it.

![Model health and connectivity tests](../images/model-health.jpg)

Each provider/model has one displayed name. Usage stays on one line until **展开 (Expand)** is selected; use **收起 (Collapse)** to close it. Expanded usage stays open during automatic refreshes. Phones and other narrow screens use cards with wrapping names, health, request times and a test button.

![Mobile model status with expanded usage](../images/model-health-mobile.png)

Decisions, formal replies, invalid-output fallback, reply review, image/audio/video captions, sticker analysis, media inspection, memory and affinity summaries, and forwarded-message summaries share one in-memory health tracker keyed by AstrBot `provider_id`. Different configurations with the same model name have independent health. Provider configuration and ordering are preserved. Embeddings, reranking and TTS are outside this conversation-model view.

## Automatic cooldown and recovery

- Two consecutive recognized connection failures, model request timeouts or server-side 5xx errors start a 60-second cooldown. A successful request clears the failure counter.
- Rate limits start cooldown immediately and honor upstream `Retry-After`. Recognized authentication, missing-model, credit and quota errors use at least ten minutes.
- Every flow skips unavailable providers during cooldown, while waiting for a probe, and during recovery tests, moving to the next provider in its **current list**. Skipped business calls consume no API request or token record, do not trigger invalid-output fallback, and never wait for a probe.
- After cooldown, a background task sends a short request asking for `OK`, with a 30-second timeout and no tools or conversation history. Failed providers are checked even without business traffic. Healthy and untested providers are not periodically tested.
- Automatic and manual tests share one probe slot per provider. Different providers run independently, so a slow endpoint cannot delay another provider's recovery.
- A nonempty text reply restores availability and clears failures. Empty output or an exception keeps the provider unavailable and extends cooldown to two, five and ten minutes. The calculated cooldown is capped at ten minutes; longer upstream delays are honored.
- Provider instance or configuration changes are checked on the next request. Restarting the plugin clears runtime health history. Unloading the plugin cancels and awaits its background probes.

Empty output, invalid XML/JSON, safety refusals, bad request parameters, context limits and local processing errors do not establish a provider outage. Health is updated at the model request boundary, before business parsing. Errors after native tool execution starts are excluded from this model's failure counter, and the existing protection against replaying tool effects remains in place.

Health admission precedes existing retry rules: request errors stay within the current list, invalid primary replies enter the separate fallback list, and invalid fallback replies end the reply. An empty fallback configuration preserves legacy output handling. If no candidate is available, each flow keeps its existing failure behavior; unavailable reply reviews still allow the draft through.

## Connectivity tests

Click **测试 (Test)** to send a short request asking for `OK`, with a 30-second timeout. Tests bypass cooldown, use no tools or conversation history, and share the single recovery-probe slot.

A nonempty text reply immediately clears failures and cooldown for every flow. Empty output or an exception does not restore health. This tests text generation, not multimodal capabilities or business XML formatting.

The visible page refreshes about every five seconds and has a manual refresh button. Automatic and manual tests are recorded under token usage type `provider_test`, with `manual_test` identifying the source. The page exposes classified failure reasons, not API keys or raw exceptions.
