"""Versioned provider defaults. Capability tests never grant subscription rights."""

CHECKED_AT = "2026-10-31T02:04:55Z"
SCOPES = ("chat", "agent", "backend", "background", "schedule")


def evidence(status, url, quote):
    return {scope: {"status": status, "source_url": url, "checked_at": CHECKED_AT, "quote": quote} for scope in SCOPES}


PRESETS = [
    {"id": "openai", "name": "OpenAI / 兼容接口", "billing_modes": ["api"], "protocols": ["chat_completions", "responses"],
     "base_urls": {"api": "https://api.openai.com/v1"}, "notes": "API 与网页订阅权益独立", "entitlements": evidence("not_applicable", "https://developers.openai.com/api/docs", "使用已开通的 API 服务")},
    {"id": "aliyun", "name": "阿里百炼", "billing_modes": ["api", "token_plan"], "protocols": ["chat_completions"],
     "base_urls": {"api": "https://dashscope.aliyuncs.com/compatible-mode/v1", "token_plan": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"},
     "notes": "Token Plan 明确限制自定义应用后端和非交互式调用", "plan_entitlements": evidence("documented_restricted", "https://help.aliyun.com/zh/model-studio/token-plan-personal-overview", "不可用于自动化脚本、自定义应用程序后端或非交互式批量调用")},
    {"id": "tencent", "name": "腾讯 TokenHub", "billing_modes": ["api", "token_plan"], "protocols": ["chat_completions"],
     "base_urls": {"api": "https://tokenhub.tencentmaas.com/v1", "token_plan": "https://tokenhub.tencentmaas.com/plan/v3"},
     "notes": "同一套餐密钥可按模型关联通用和 Hy 两个额度池；本 Bot 套餐用途待确认", "plan_entitlements": evidence("needs_confirmation", "https://cloud.tencent.com/document/product/1823/130076", "个人套餐用途需按实际场景核对，不自动转按量")},
    {"id": "volcengine", "name": "火山方舟", "billing_modes": ["api", "agent_plan"], "protocols": ["chat_completions", "responses"],
     "base_urls": {"api": "https://ark.cn-beijing.volces.com/api/v3", "agent_plan": "https://ark.cn-beijing.volces.com/api/plan/v3"},
     "notes": "需核实自建 AI 工具范围及厂商超额后付费开关", "plan_entitlements": evidence("needs_confirmation", "https://ark.volcengine.com/region:cn-beijing/docs/agent-plan-personal-plan-overview", "非 AI 工具调用受限，开启超额后付费可在同一入口继续计费")},
    {"id": "minimax", "name": "MiniMax", "billing_modes": ["api", "m_plan"], "protocols": ["chat_completions"],
     # https://platform.minimaxi.com/docs/api-reference/text-openai-api.md
     "base_urls": {"api": "https://api.minimax.cn/v1", "m_plan": "https://api.minimax.cn/v1"},
     "notes": "订阅密钥与普通密钥独立；套餐用尽可能继续抵扣已购积分包", "plan_entitlements": evidence("needs_confirmation", "https://platform.minimax.cn/docs/m-plan/faq", "个人交互式 AI 工具；无人值守 Bot 用途仍需核实")},
    {"id": "kimi", "name": "Kimi Platform", "billing_modes": ["api", "coding_plan"], "protocols": ["chat_completions", "responses"],
     "base_urls": {"api": "https://api.moonshot.cn/v1", "coding_plan": "https://api.kimi.com/coding/v1"},
     "notes": "通用自建产品使用 Kimi Platform；不伪装客户端", "plan_entitlements": evidence("documented_restricted", "https://www.kimi.com/code/docs/en/", "Kimi Code 面向编程场景，自有产品请访问 Kimi Platform")},
    {"id": "zhipu", "name": "智谱标准 API", "billing_modes": ["api", "coding_plan"], "protocols": ["chat_completions"],
     "base_urls": {"api": "https://open.bigmodel.cn/api/paas/v4", "coding_plan": "https://open.bigmodel.cn/api/coding/paas/v4"},
     "notes": "自建机器人须使用标准 API", "plan_entitlements": evidence("documented_restricted", "https://docs.bigmodel.cn/cn/coding-plan/faq", "自建应用、网站、机器人、SaaS 请使用标准 API")},
    {"id": "deepseek", "name": "DeepSeek", "billing_modes": ["api"], "protocols": ["chat_completions"],
     "base_urls": {"api": "https://api.deepseek.com/v1"}, "notes": "能力以当前连接实测为准"},
    {"id": "custom", "name": "自定义兼容服务", "billing_modes": ["api", "subscription"], "protocols": ["chat_completions", "responses"],
     "base_urls": {}, "notes": "明确填写协议、入口、模型及使用范围，不接收网页 Cookie"},
]


def preset(provider: str) -> dict:
    return next((x for x in PRESETS if x["id"] == provider), PRESETS[-1])


def default_entitlements(provider: str, billing_mode: str) -> dict:
    from copy import deepcopy
    p = preset(provider)
    if billing_mode != "api":
        return deepcopy(p.get("plan_entitlements", evidence("needs_confirmation", "", "套餐用途尚未核实")))
    return deepcopy(p.get("entitlements", evidence("not_applicable", "", "普通 API；按服务协议使用")))
