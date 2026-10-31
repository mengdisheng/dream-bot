from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from .errors import BotError
from .net import normalize_base
from .presets import SCOPES, default_entitlements, preset
from .store import Store, dumps, now, uid

CAPS = ("auth", "text", "stream", "tools", "vision")
PROTOCOLS = ("chat_completions", "responses")
OVERAGE_LIMITS = {"max_tasks":3,"max_tools":8,"max_tokens":100000,"max_active_seconds":300}


class Connections:
    def __init__(self, store: Store):
        self.s = store

    def get(self, identity: str, *, private=False, revision=None) -> dict:
        row = self.s.one("SELECT * FROM model_connections WHERE id=?", (identity,))
        if row is None:
            raise BotError("not_found", "模型连接不存在", 404)
        for key in ("capabilities", "options"):
            row[key] = json.loads(row[key])
        detail = self.s.one("SELECT * FROM connection_details WHERE connection_id=?", (identity,))
        if detail:
            row.update(detail)
            entry = self.s.one("SELECT * FROM billing_entries WHERE id=?", (detail["billing_entry_id"],))
            row["entitlements"] = json.loads(entry["entitlements"])
            row["provider_overage_state"] = entry["overage_state"]
            row["provider_overage_kind"] = entry["overage_kind"]
            cred = self.s.one("SELECT secret_revision FROM credentials WHERE id=?", (detail["credential_id"],))
            row["secret_revision"] = cred["secret_revision"]
            if private:
                authoritative_secret = self.s.one("SELECT secret FROM credentials WHERE id=?",(detail["credential_id"],))
                row["secret"] = authoritative_secret["secret"]
        row["quota_bindings"] = self.s.all("SELECT b.quota_group_id,b.model_ids,g.unit,g.subscription_ref FROM quota_bindings b JOIN quota_groups g ON b.quota_group_id=g.id WHERE b.connection_id=?", (identity,))
        for binding in row["quota_bindings"]:
            binding["model_ids"] = json.loads(binding["model_ids"])
            binding["windows"] = self.s.all("SELECT * FROM quota_windows WHERE quota_group_id=?", (binding["quota_group_id"],))
        if revision is not None and revision != row["revision"]:
            version = self.s.one("SELECT config FROM connection_versions WHERE connection_id=? AND revision=?", (identity, revision))
            if version is None:
                raise BotError("connection_changed", "任务绑定的模型配置版本已不可用", 409)
            saved = json.loads(version["config"])
            # Secret rotation is permitted at the next request; destination is immutable.
            row.update(saved)
            if saved.get("validated_secret_revision") != row.get("secret_revision"):
                row["capabilities"] = {key:"not_tested" for key in CAPS}
        row["has_api_key"] = bool(row["secret"])
        if private:
            if not row["secret"]:
                raise BotError("missing_api_key", "请重新配置模型密钥", 409)
            row["api_key"] = self.s.cipher.decrypt(row["secret"].encode()).decode()
        del row["secret"]
        row["enabled"] = bool(row["enabled"])
        row["overage_approval"] = self.overage_status(row)
        return row

    @staticmethod
    def approval_key(identity):
        return "overage_approval:"+identity

    def approval_policy(self, connection):
        fields = ("id","revision","secret_revision","enabled","provider","billing_mode",
                  "protocol","base_url","model_id","options","entitlements",
                  "provider_overage_state","provider_overage_kind")
        value = {key:connection.get(key) for key in fields}
        value["deployment_epoch"] = self.s.config.deployment_epoch
        return hashlib.sha256(dumps(value).encode()).hexdigest()

    def overage_status(self, connection):
        raw = self.s.meta(self.approval_key(connection["id"]))
        approval = json.loads(raw) if raw else None
        eligible = (connection["billing_mode"] != "api" and connection.get("enabled") and
                    connection.get("provider_overage_state") == "enabled" and
                    connection.get("provider_overage_kind") == "purchased_credits")
        reason = "missing"
        if approval:
            if approval.get("revoked"):
                reason = "revoked"
            elif approval["expires_unix"] <= time.time():
                reason = "expired"
            elif approval["policy"] != self.approval_policy(connection):
                reason = "connection_changed"
            elif not eligible:
                reason = "ineligible"
            else:
                reason = None
        count = len(approval["task_ids"]) if approval else 0
        maximum = approval["max_tasks"] if approval else 0
        active = reason is None
        return {"active":active,"can_create_task":active and count < maximum,
                "kind":approval["kind"] if approval else None,"reason":reason,
                "expires_at":approval["expires_at"] if approval else None,
                "max_tasks":maximum,"tasks_used":count,"remaining_tasks":max(0,maximum-count),
                "budgets":approval["budgets"] if approval else None}

    def approve_overage(self, identity, body):
        with self.s.tx() as db:
            self.s.writable(db)
            connection = self.get(identity)
            if (type(body.get("expected_revision")) is not int or
                    type(body.get("expected_secret_revision")) is not int or
                    body.get("expected_revision") != connection["revision"] or
                    body.get("expected_secret_revision") != connection.get("secret_revision")):
                raise BotError("revision_conflict","模型或密钥版本已经更新",409)
            if body.get("kind") != "purchased_credits" or body.get("acknowledge_purchased_credits") is not True:
                raise BotError("overage_acknowledgement_required","须明确授权本次测试抵扣已购积分包",409)
            if (connection["billing_mode"] == "api" or not connection["enabled"] or
                    connection.get("provider_overage_state") != "enabled" or
                    connection.get("provider_overage_kind") != "purchased_credits"):
                raise BotError("overage_ineligible","仅已确认抵扣已购积分包的套餐可获短时授权",409)
            self.base_allowed(connection,"chat")
            seconds = body.get("expires_in_seconds",3600)
            values = {key:body.get(key,limit) for key,limit in OVERAGE_LIMITS.items()}
            if type(seconds) is not int or not 1 <= seconds <= 3600 or any(
                    type(values[key]) is not int or not 1 <= values[key] <= limit
                    for key,limit in OVERAGE_LIMITS.items()):
                raise BotError("invalid_overage_budget","短时授权期限或预算超出允许范围")
            expiry = time.time()+seconds
            approval = {"id":uid(),"kind":"purchased_credits","policy":self.approval_policy(connection),
                        "connection_revision":connection["revision"],"secret_revision":connection["secret_revision"],
                        "deployment_epoch":self.s.config.deployment_epoch,
                        "created_at":now(),"expires_unix":expiry,
                        "expires_at":datetime.fromtimestamp(expiry,timezone.utc).isoformat().replace("+00:00","Z"),
                        "max_tasks":values.pop("max_tasks"),"budgets":values,"task_ids":[],"revoked":False}
            self.s.setmeta(self.approval_key(identity),dumps(approval),db)
        return self.get(identity)

    def revoke_overage(self, identity):
        with self.s.tx() as db:
            self.s.writable(db)
            self.get(identity)
            raw = self.s.meta(self.approval_key(identity),db=db)
            if raw:
                approval = json.loads(raw)
                approval["revoked"] = True
                self.s.setmeta(self.approval_key(identity),dumps(approval),db)
        return self.get(identity)

    def delete(self, identity: str, expected_revision: int) -> dict:
        with self.s.tx() as db:
            self.s.writable(db)
            connection = db.execute("SELECT revision FROM model_connections WHERE id=?",(identity,)).fetchone()
            if connection is None:
                raise BotError("not_found","模型连接不存在",404)
            if type(expected_revision) is not int or expected_revision != connection["revision"]:
                raise BotError("revision_conflict","模型配置已经更新，请重新读取后确认删除",409)
            if (db.execute("SELECT 1 FROM tasks WHERE connection_id=? AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1",(identity,)).fetchone()
                    or db.execute("SELECT 1 FROM model_calls WHERE connection_id=? AND status='dispatched' LIMIT 1",(identity,)).fetchone()):
                raise BotError("connection_in_use","该连接仍有关联的未结束任务或模型请求，请先完成或取消任务并等待请求结束后再删除",409)
            detail = db.execute("SELECT * FROM connection_details WHERE connection_id=?",(identity,)).fetchone()
            cleared = []
            for role in ("chat","agent"):
                key = "default_"+role+"_connection"
                if self.s.meta(key,db=db) == identity:
                    db.execute("DELETE FROM meta WHERE key=?",(key,))
                    cleared.append(role)
            db.execute("DELETE FROM meta WHERE key=?",(self.approval_key(identity),))
            # These bindings no longer grant anything once the connection is gone.
            # Task bodies, usage, results and versioned nonsecret configs remain.
            db.execute("DELETE FROM meta WHERE key IN (SELECT 'overage_task:'||id FROM tasks WHERE connection_id=?)",(identity,))
            db.execute("DELETE FROM quota_bindings WHERE connection_id=?",(identity,))
            db.execute("DELETE FROM connection_details WHERE connection_id=?",(identity,))
            db.execute("DELETE FROM model_connections WHERE id=?",(identity,))
            if detail:
                # Other model profiles may share an endpoint or credential.
                # Keep shared objects and all billing/quota history intact.
                db.execute("DELETE FROM model_profiles WHERE id=? AND NOT EXISTS (SELECT 1 FROM connection_details WHERE model_profile_id=?)",
                           (detail["model_profile_id"],detail["model_profile_id"]))
                db.execute("DELETE FROM endpoints WHERE id=? AND NOT EXISTS (SELECT 1 FROM connection_details WHERE endpoint_id=?) AND NOT EXISTS (SELECT 1 FROM model_profiles WHERE endpoint_id=?)",
                           (detail["endpoint_id"],detail["endpoint_id"],detail["endpoint_id"]))
                db.execute("DELETE FROM credentials WHERE id=? AND NOT EXISTS (SELECT 1 FROM connection_details WHERE credential_id=?) AND NOT EXISTS (SELECT 1 FROM endpoints WHERE credential_id=?)",
                           (detail["credential_id"],detail["credential_id"],detail["credential_id"]))
            self.s.activity(db)
        return {"deleted":True,"id":identity,"cleared_default_roles":cleared}

    def needs_overage_approval(self, connection):
        live = self.get(connection["id"])
        return any(c["billing_mode"] != "api" and c.get("provider_overage_state") != "confirmed_disabled"
                   for c in (connection,live))

    def live_overage_approval(self, connection, *, role, schedule=False):
        live = self.get(connection["id"])
        self.base_allowed(live,role,schedule=schedule)
        if schedule:
            raise BotError("overage_background_forbidden","短时积分包授权不能用于定时或后台任务",409)
        if connection["revision"] != live["revision"] or not live["overage_approval"]["active"]:
            raise BotError("overage_unconfirmed","本次积分包授权已失效或尚未授予",409)
        return json.loads(self.s.meta(self.approval_key(connection["id"])))

    def enroll_overage_task(self, connection, task_id, budgets, db, *, role, schedule=False):
        if not self.needs_overage_approval(connection):
            return budgets
        approval = self.live_overage_approval(connection,role=role,schedule=schedule)
        if task_id not in approval["task_ids"]:
            if len(approval["task_ids"]) >= approval["max_tasks"]:
                raise BotError("overage_task_limit","本次积分包测试授权的任务次数已用完",409)
            approval["task_ids"].append(task_id)
            self.s.setmeta(self.approval_key(connection["id"]),dumps(approval),db)
            self.s.setmeta("overage_task:"+task_id,dumps({"approval_id":approval["id"],"connection_id":connection["id"]}),db)
        return {key:min(value,approval["budgets"][key]) for key,value in budgets.items()}

    def task_overage_approval(self, connection, task):
        raw = self.s.meta("overage_task:"+task["id"])
        if not raw and not self.needs_overage_approval(connection):
            return None
        binding = json.loads(raw) if raw else None
        if not binding or binding["connection_id"] != connection["id"]:
            raise BotError("overage_task_not_approved","此任务未登记在当前积分包测试授权内",409)
        approval = self.live_overage_approval(connection,role=task.get("execution_mode","agent"),schedule=bool(task["schedule_id"]))
        if task["id"] not in approval["task_ids"] or binding["approval_id"] != approval["id"]:
            raise BotError("overage_task_not_approved","此任务未登记在当前积分包测试授权内",409)
        return approval

    def cap_task_budgets(self, task, values):
        connection = self.get(task["connection_id"],revision=task["connection_revision"])
        approval = self.task_overage_approval(connection,task)
        if approval is None:
            return values
        return {key:min(value,approval["budgets"][key]) for key,value in values.items()}

    def authorize_task_call(self, connection, task):
        approval = self.task_overage_approval(connection,task)
        if approval is None:
            return
        limits = approval["budgets"]
        if (task["token_count"] >= min(task["max_tokens"],limits["max_tokens"]) or
                task["active_seconds"] >= min(task["max_active_seconds"],limits["max_active_seconds"])):
            raise BotError("overage_budget_exhausted","本次积分包测试任务已达到授权预算",409)

    def save(self, body: dict, identity: str | None = None) -> dict:
        editing = identity is not None
        previous = self.get(identity, private=False) if identity else {}
        if identity and body.get("expected_revision") != previous["revision"]:
            raise BotError("revision_conflict", "模型配置已经更新", 409)
        value = {**previous, **body}
        provider = value.get("provider", "custom")
        billing = value.get("billing_mode", "api")
        protocol = value.get("protocol", "chat_completions")
        if protocol not in PROTOCOLS:
            raise BotError("invalid_protocol", "请选择 Chat Completions 或 Responses")
        base = normalize_base(value.get("base_url") or preset(provider).get("base_urls", {}).get(billing, ""), protocol)
        model = str(value.get("model_id", "")).strip()
        if not model or len(model) > 256:
            raise BotError("invalid_model", "请填写有效模型 ID")
        if "api_key" in body and not body["api_key"]:
            raise BotError("invalid_api_key", "密钥不可为空；清除密钥请使用明确的清除操作")
        if not identity and not body.get("api_key"):
            raise BotError("missing_api_key", "请填写 API 密钥")
        options = value.get("options", {})
        allowed = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens", "reasoning_effort", "reasoning", "thinking", "enable_thinking"}
        if not isinstance(options, dict) or set(options) - allowed:
            raise BotError("unsupported_parameter", "高级参数不在允许列表")
        identity = identity or uid()
        account = value.get("account_id") or uid()
        credential = value.get("credential_id") or uid()
        entry = value.get("billing_entry_id") or uid()
        endpoint = previous.get("endpoint_id") or uid()
        profile = previous.get("model_profile_id") or uid()
        revision = previous.get("revision", 0) + 1
        entitlements = default_entitlements(provider, billing)
        # Owner may attach verifiable scope evidence for unknown/custom products,
        # but cannot override a provider's documented restriction.
        supplied = value.get("entitlements", {})
        for scope in SCOPES:
            candidate = supplied.get(scope)
            if candidate and entitlements[scope]["status"] != "documented_restricted":
                if candidate.get("status") not in ("documented_supported", "needs_confirmation", "not_applicable", "documented_restricted"):
                    raise BotError("invalid_entitlement", "用途状态无效")
                if candidate["status"] == "documented_supported" and (not str(candidate.get("source_url", "")).startswith("https://") or not candidate.get("quote")):
                    raise BotError("missing_evidence", "用途允许须附官方 HTTPS 资料与短原文")
                entitlements[scope] = candidate
        overage = value.get("provider_overage_state", "unknown" if billing != "api" else "enabled")
        kind = value.get("provider_overage_kind", "unknown" if billing != "api" else "postpaid")
        if overage not in ("unknown", "enabled", "confirmed_disabled") or kind not in ("none", "unknown", "postpaid", "purchased_credits"):
            raise BotError("invalid_overage", "超额消费状态无效")
        with self.s.tx() as db:
            self.s.writable(db)
            old = db.execute("SELECT secret,revision FROM model_connections WHERE id=?", (identity,)).fetchone()
            # An old edit may have waited behind a deletion or another save.
            # Never resurrect a deleted connection or overwrite a newer revision.
            if editing:
                if old is None:
                    raise BotError("not_found","模型连接不存在",404)
                if body.get("expected_revision") != old["revision"]:
                    raise BotError("revision_conflict","模型配置已经更新",409)
            secret = self.s.cipher.encrypt(body["api_key"].encode()).decode() if "api_key" in body else (old[0] if old else "")
            if body.get("clear_api_key") is True:
                secret = ""
            if old and ("api_key" in body or body.get("clear_api_key")):
                db.execute("UPDATE model_connections SET secret=?,capabilities=?,validated_at=NULL WHERE id IN (SELECT connection_id FROM connection_details WHERE credential_id=?)",(secret,dumps({x:"not_tested" for x in CAPS}),credential))
            capabilities = {x: "not_tested" for x in CAPS}
            db.execute("INSERT INTO model_connections(id,display_name,provider,region,billing_mode,protocol,base_url,model_id,secret,capabilities,options,revision,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,provider=excluded.provider,region=excluded.region,billing_mode=excluded.billing_mode,protocol=excluded.protocol,base_url=excluded.base_url,model_id=excluded.model_id,secret=excluded.secret,capabilities=excluded.capabilities,options=excluded.options,revision=excluded.revision,enabled=excluded.enabled,validated_at=NULL", (identity,value.get("display_name",model),provider,value.get("region","cn"),billing,protocol,base,model,secret,dumps(capabilities),dumps(options),revision,int(value.get("enabled",True)),now()))
            db.execute("INSERT INTO provider_accounts VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING", (account,provider,value.get("account_label","个人账户"),value.get("region","cn")))
            db.execute("INSERT INTO credentials VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,secret_revision=credentials.secret_revision+1", (credential,account,billing,1,secret))
            db.execute("INSERT INTO billing_entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET product=excluded.product,billing_mode=excluded.billing_mode,entitlements=excluded.entitlements,overage_state=excluded.overage_state,overage_kind=excluded.overage_kind", (entry,account,value.get("product",billing),billing,dumps(entitlements),overage,kind))
            db.execute("INSERT INTO endpoints VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET protocol=excluded.protocol,base_url=excluded.base_url,revision=excluded.revision", (endpoint,credential,entry,protocol,base,value.get("region","cn"),revision))
            db.execute("INSERT INTO model_profiles VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET model_id=excluded.model_id,parameter_policy=excluded.parameter_policy", (profile,endpoint,model,dumps(options)))
            db.execute("INSERT OR REPLACE INTO connection_details VALUES(?,?,?,?,?,?)", (identity,account,credential,entry,endpoint,profile))
            db.execute("DELETE FROM quota_bindings WHERE connection_id=?", (identity,))
            for binding in value.get("quota_bindings", []):
                group = binding.get("quota_group_id") or uid()
                unit = binding.get("unit", "token")
                if unit not in ("token", "requests", "credits", "AFP", "unknown"):
                    raise BotError("invalid_quota_unit", "额度单位无效")
                db.execute("INSERT INTO quota_groups VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING", (group,account,binding.get("subscription_ref",group),unit,binding.get("limit_source","owner_configuration")))
                db.execute("INSERT INTO quota_bindings VALUES(?,?,?)", (identity,group,dumps(binding.get("model_ids",[model]))))
            config = {"protocol":protocol,"base_url":base,"model_id":model,"options":options,"revision":revision,"provider":provider,"billing_mode":billing,"entitlements":entitlements,"provider_overage_state":overage,"provider_overage_kind":kind,"capabilities":capabilities,"validated_at":None}
            db.execute("INSERT INTO connection_versions VALUES(?,?,?)", (identity,revision,dumps(config)))
            self.s.activity(db)
        return self.get(identity)

    def base_allowed(self, connection: dict, role: str, *, schedule=False):
        if not connection["enabled"]:
            raise BotError("connection_disabled", "该模型连接已停用", 409)
        scopes = ["backend", "schedule" if schedule else ("agent" if role == "agent" else "chat")]
        for scope in scopes:
            status = connection.get("entitlements", {}).get(scope, {}).get("status", "needs_confirmation")
            if status not in ("documented_supported", "not_applicable"):
                raise BotError("entitlement_unconfirmed", "当前产品用途受限或尚未获得官方允许依据，请配置独立标准 API 连接", 409)
        if role == "agent" and connection["capabilities"].get("tools") != "passed":
            raise BotError("tools_not_verified", "自动执行模型须通过完整工具往返验证", 409)
        if connection["capabilities"].get("text") != "passed":
            raise BotError("model_not_verified", "请先验证模型文本能力", 409)

    def allowed(self, connection: dict, role: str, *, schedule=False):
        self.base_allowed(connection,role,schedule=schedule)
        if self.needs_overage_approval(connection):
            self.live_overage_approval(connection,role=role,schedule=schedule)

    def bind(self, role="agent", schedule=False) -> dict:
        identity = self.s.meta("default_" + role + "_connection")
        if not identity:
            raise BotError("model_not_configured", "请先配置并验证默认模型", 409)
        connection = self.get(identity)
        self.allowed(connection, role, schedule=schedule)
        return connection

    def quota_status(self, connection: dict) -> dict:
        blocking = []
        for binding in connection.get("quota_bindings",[]):
            if binding["model_ids"] and connection["model_id"] not in binding["model_ids"]:
                continue
            for window in binding["windows"]:
                if window["remaining"] is not None and window["remaining"] <= 0:
                    # Expired observations cannot prove a new window's balance.
                    if window.get("reset_at"):
                        from datetime import datetime
                        if datetime.fromisoformat(window["reset_at"].replace("Z","+00:00")).timestamp() <= __import__('time').time():
                            continue
                    blocking.append(window)
        reset = max((w["reset_at"] for w in blocking),default=None) if blocking and all(w["trusted"] and w["reset_at"] for w in blocking) else None
        return {"blocked":bool(blocking),"resume_at":reset,"windows":blocking}
