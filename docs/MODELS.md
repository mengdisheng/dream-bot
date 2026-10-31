# Models, protocols and subscription plans

The application separates **provider account → credential → billing entry → endpoint → model profile → quota binding**. Reusing a key does not prove that two connections share quota. API protocol compatibility, tested capabilities, permitted usage and actual supplier overage behavior are separate facts.

## Configure a connection

1. Open **Settings → Models → Add connection**. Choose a built-in preset or a custom OpenAI-compatible endpoint.
2. Enter the documented region, billing product, model ID and base URL. Use the service root (for example the provider's documented `/v1`), not a full `/chat/completions` URL. Do not guess an alternate billable route.
3. Choose **Chat Completions** or **Responses** only if supported by that actual endpoint. Enter the key in the settings form; it stays server-side. An empty saved-key edit preserves the existing secret.
4. Save. Saving does not invoke a model. Use explicit validation separately: it can make up to four bounded requests over at most 120 seconds and consume real credits. Text, stream, tools and vision have distinct results; untested is not passed.
5. Bind the default chat/agent roles only when the connection satisfies the role's capability and usage gates. An agent requires a complete tool roundtrip, not just a successful text response.

The preset catalog includes Alibaba Bailian, Tencent TokenHub, Volcengine Ark, MiniMax, Kimi, Zhipu, DeepSeek and custom compatible services. This is configurable integration code, not a claim that every provider, model or plan has passed real validation. Verify current URLs and terms with each provider. Model services may charge even though Dream Bot's code is fully open source.

## Token/Coding Plans

Subscription keys and standard API keys keep separate endpoints and entitlement records. A successful API call cannot override provider terms. Background and scheduled use remain blocked when the permitted scope is restricted or unknown. The application does not silently switch provider, region or billing endpoint when quota is exhausted.

Supplier-internal fallback to purchased credits can be distinct from a switch to the ordinary API account. Unknown overage settings remain unknown. MiniMax subscription semantics in particular must be checked against its current [FAQ](https://platform.minimax.cn/docs/m-plan/faq.md) and [supported tools](https://platform.minimax.cn/docs/m-plan/other-tools.md); do not infer a spending cap from a remaining-quota percentage.

An explicit owner-authorized temporary purchased-credit grant is bounded to at most one hour and three tasks, with per-task ceilings of eight tools, 100,000 tokens and 300 active seconds. It is bound to connection/secret revisions and deployment epoch and cannot authorize background/scheduled use. Expiry, revocation or relevant edits block subsequent requests. These are application admission limits, not a supplier invoice guarantee. No grant is created automatically.

## Protocol and recovery behavior

Chat Completions and Responses have separate adapters and bounded streaming parsers. Provider reasoning/opaque continuation items are preserved with their own execution/checkpoint; another conversation/model does not inherit them indiscriminately. Full original audit/checkpoints remain available. The current implementation records content-free input-byte measurements and actual usage where provided; it does not automatically prune context or add paid summary calls.

Request errors retain quota/auth/billing distinctions. No model call is accepted merely because the browser request timed out. Reconcile using the original request ID before retrying a submission that may already have been committed.
