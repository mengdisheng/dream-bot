# Dream Bot · 梦 Bot

**会話を中心にした、セルフホスト型の個人エージェント。必要なときだけ実行環境を作成します。**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

Dream Bot は会話、ファイル、ブラウザープロファイルを自分のサーバーに保存します。小さなホストゲートウェイは常駐し、作業用 Docker コンテナは最大 1 個だけ起動します。安全に停止できる状態で 300 秒間アイドルになると、コンテナを実際に削除します。ブラウザーの表示・手動操作とファイルのプレビューは会話内のパネルで利用できます。

## 主な機能

- 永続的な会話と実行履歴、明示的な承認、不確定な外部操作の照合。
- Chromium、手動引き継ぎ、ファイル処理、メモリー、スケジュール、暗号化バックアップ。
- 設定可能な Chat Completions / Responses 互換 API。Token/Coding Plan は実際の提供条件を満たす場合に利用でき、接続成功だけでは契約上の権利を証明しません。
- 自動読み取りや読み取り専用の閲覧は、環境を起動したり寿命を延ばしたりしません。データ保存とプロセスメモリーの復元は別物です。

フロントエンド、バックエンド、ホスト管理、サンドボックス、デプロイ、テストの独自コードは MIT で公開しています。別の非公開コントロールプレーンはありません。モデルサービスの料金と第三者ライセンスは別途適用されます。

## 導入と状態

Linux amd64、systemd/cgroup v2、Docker と、AppArmor・Landlock・seccomp・Chromium のユーザー名前空間を利用できるカーネルが必要です。メモリーは 4 GB 以上を目安にしてください。Go、Python、TypeScript/React、SQLite を使用します。

実験的なソースリリースです。独立したクリーンインストール、ARM64、全プロバイダー、完全な障害・復元検証は未完了です。アプリの UI は主に中国語で、この README はドキュメントの翻訳です。共有の初期パスワードや本番の認証情報は含みません。

[導入](../INSTALL.md) · [詳細アーキテクチャー](../ARCHITECTURE.md) · [モデル](../MODELS.md) · [検証範囲](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
