import { useEffect, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { KeyRound, ShieldCheck, Download, ArrowRight } from "lucide-react";
import { api, localDownload, setCsrf } from "./api";
import { enrollmentSchema, loginSchema, type Owner } from "./types";
import { ErrorNotice, Field } from "./components";
import { useAction } from "./hooks";

export function Auth({
  initialized,
  onAuthenticated,
}: {
  initialized: boolean;
  onAuthenticated: (owner: Owner) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");
  const [code, setCode] = useState("");
  const [recoveryMode, setRecoveryMode] = useState(false);
  const [enrollment, setEnrollment] =
    useState<ReturnType<typeof enrollmentSchema.parse>>();
  const [qr, setQr] = useState("");
  const [completed, setCompleted] =
    useState<ReturnType<typeof loginSchema.parse>>();
  const [saved, setSaved] = useState(false);
  const action = useAction();
  const secure =
    location.protocol === "https:" ||
    ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  useEffect(() => {
    let current = true;
    if (enrollment)
      void QRCode.toDataURL(enrollment.totp_uri, { margin: 1, width: 200 })
        .then((value) => {
          if (current) setQr(value);
        })
        .catch(() => {
          if (current) setQr("");
        });
    return () => {
      current = false;
    };
  }, [enrollment]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!secure) return;
    if (!initialized && !enrollment) {
      const result = await action.run(() =>
        api("/api/auth/setup/enrollment", enrollmentSchema, {
          method: "POST",
          body: { setup_token: setupToken, username },
        }),
      );
      if (result) setEnrollment(result);
      return;
    }
    const body = initialized
      ? {
          username,
          password,
          ...(recoveryMode ? { recovery_code: code } : { totp_code: code }),
        }
      : {
          setup_token: setupToken,
          enrollment_id: enrollment?.enrollment_id,
          username,
          password,
          totp_code: code,
        };
    const result = await action.run(() =>
      api(initialized ? "/api/auth/login" : "/api/auth/setup", loginSchema, {
        method: "POST",
        body,
      }),
    );
    if (result) {
      setPassword("");
      setCode("");
      setSetupToken("");
      setEnrollment(undefined);
      setQr("");
      setCsrf(result.csrf_token);
      if (result.recovery_codes?.length) setCompleted(result);
      else onAuthenticated(result.owner);
    }
  }
  if (completed)
    return (
      <main className="auth-layout">
        <section className="auth-story">
          <ShieldCheck size={32} />
          <span className="eyebrow">仅显示这一次</span>
          <h1>
            保管你的
            <br />
            恢复码。
          </h1>
          <p>丢失验证器时，可以使用一个恢复码登录。每个码只能使用一次。</p>
        </section>
        <section className="auth-form">
          <h2>账号已建立</h2>
          <p>将恢复码保存在你能独立取回的位置。关闭此页后无法再次查看。</p>
          <pre className="recovery-codes">
            {completed.recovery_codes?.join("\n")}
          </pre>
          <button
            className="button secondary"
            onClick={() =>
              localDownload(
                completed.recovery_codes?.join("\n") ?? "",
                "personal-bot-recovery-codes.txt",
              )
            }
          >
            <Download size={16} />
            下载恢复码
          </button>
          <label className="check">
            <input
              type="checkbox"
              checked={saved}
              onChange={(e) => setSaved(e.target.checked)}
            />
            我已经保存好恢复码
          </label>
          <button
            className="button"
            disabled={!saved}
            onClick={() => onAuthenticated(completed.owner)}
          >
            进入工作台
            <ArrowRight size={16} />
          </button>
        </section>
      </main>
    );
  return (
    <main className="auth-layout">
      <section className="auth-story">
        <div className="brand-mark">梦</div>
        <span className="eyebrow">PERSONAL WORKSPACE / 01</span>
        <h1>
          把想做的事，
          <br />
          交给自己的
          <br />
          <em>工作台。</em>
        </h1>
        <p>
          任务、浏览器、文件与记忆集中在这里。环境按需启动，空闲后自动回收。
        </p>
        <span className="auth-foot">
          <ShieldCheck size={16} />
          单一所有者 · 数据独立保存
        </span>
      </section>
      <section className="auth-form">
        <KeyRound size={25} />
        <span className="eyebrow">{initialized ? "欢迎回来" : "首次设置"}</span>
        <h2>{initialized ? "登录你的 Bot" : "建立唯一所有者"}</h2>
        <p>
          {initialized
            ? "登录和查看环境状态不需要启动工作容器。"
            : "使用安装时生成的一次性凭据，设置账号与双重验证。"}
        </p>
        {!secure && (
          <div role="alert" className="notice notice-error">
            请通过受信任的 HTTPS 地址打开此页后，再输入登录凭据。
          </div>
        )}
        <ErrorNotice error={action.error} />
        <form onSubmit={(e) => void submit(e)}>
          {!initialized && (
            <Field label="一次性初始化凭据">
              <input
                type="password"
                value={setupToken}
                autoComplete="off"
                onChange={(e) => setSetupToken(e.target.value)}
                required
                disabled={!!enrollment}
              />
            </Field>
          )}
          <Field label="用户名">
            <input
              value={username}
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
              required
              maxLength={80}
              disabled={!!enrollment}
            />
          </Field>
          {(initialized || enrollment) && (
            <>
              <Field label="密码">
                <input
                  type="password"
                  value={password}
                  autoComplete={
                    initialized ? "current-password" : "new-password"
                  }
                  minLength={initialized ? 1 : 12}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </Field>
              {enrollment && (
                <div className="totp">
                  <strong>将此账号加入验证器</strong>
                  {qr && (
                    <img
                      src={qr}
                      alt="本地生成的双重验证二维码"
                      width="200"
                      height="200"
                    />
                  )}
                  <details>
                    <summary>手动输入密钥</summary>
                    <code className="break-all">{enrollment.totp_secret}</code>
                  </details>
                  <small>二维码与密钥只在当前页面生成和显示。</small>
                </div>
              )}
              <Field label={recoveryMode ? "恢复码" : "验证器动态码"}>
                <input
                  value={code}
                  inputMode={recoveryMode ? "text" : "numeric"}
                  autoComplete="one-time-code"
                  onChange={(e) => setCode(e.target.value)}
                  required
                />
              </Field>
            </>
          )}
          <button
            className="button w-full"
            type="submit"
            disabled={action.busy || !secure}
          >
            {action.busy
              ? "正在处理…"
              : initialized
                ? "安全登录"
                : enrollment
                  ? "完成设置"
                  : "设置双重验证"}
            <ArrowRight size={16} />
          </button>
          {initialized && (
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setRecoveryMode(!recoveryMode);
                setCode("");
              }}
            >
              {recoveryMode ? "使用验证器登录" : "改用恢复码"}
            </button>
          )}
        </form>
      </section>
    </main>
  );
}
