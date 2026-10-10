# Release Notes / リリースノート

📖 日本語は[下記](#日本語)、English is [below](#english)。

---

## 日本語

### v1.0.3 (2026-10-11)

**バグ修正**
- **Keycloakログインが`mismatching_state: CSRF Warning!`で失敗する不具合を修正**: ログイン開始時(Keycloak/アプリ個別OIDCの両方)にセッションを`permanent`にするタイミングが認証成功後のみだったため、Keycloakへのリダイレクト中に`state`を保持するクッキーに有効期限が付いておらず、タブがバックグラウンドで長時間放置されるなどのケースでブラウザがそのクッキーだけを先に破棄してしまうことがありました。初回ログインは成立するが、しばらく経ってから再度ログインしようとすると失敗する、という形で表面化していました。

**アップグレード方法**: `git pull` の後、`sudo ./scripts/update.sh` を実行してください。

---

### v1.0.2 (2026-08-29)

**新機能**
- **セッションのアイドルタイムアウトを追加**: これまでログインセッションに有効期限が一切なく、ブラウザを閉じるまで有効なままでした。今回からログイン後の無操作時間を分単位で設定できるようになり（デフォルト30分）、Configページから再起動不要で変更できます。Keycloak・ローカルアカウント・アプリ個別OIDCログインのすべてに共通して適用され、操作を続けている間は延長され続けます（固定の有効期限ではなく、アイドルタイムアウトです）。

**アップグレード方法**: `git pull` の後、`sudo ./scripts/update.sh` を実行してください。

---

### v1.0.1 (2026-08-11)

**新機能**
- **管理画面の日英対応 (i18n)**: ブラウザの言語設定を自動検出し、日本語/英語を自動で切り替えます。サイドバー下部の切り替えボタンでいつでも手動切替も可能です（選択はCookieに保存されます）。全画面が翻訳対象です。
- **Configページに「エクスポート」セクションを追加**: これまでダッシュボードのApp一覧の横にあった「全体をエクスポート」を移動し、ドロップダウンで「全体」または個別のApp/Site/Proxy/Cron Job/Containerを選んでダウンロードできるようにしました。

**セキュリティ修正**
- 軽微なセキュリティ修正を含みます。

**細かい修正**
- インストール/更新スクリプト (`install.sh`/`update.sh`) が新規追加した`i18n.py`をコピーしていなかった不具合を修正（未更新のノードでは`sudo ./scripts/update.sh`の再実行が必要です）。
- サイドバーのレイアウトを調整（不要になった`ns:`表示を削除し、言語切り替えとバージョン表示を1行に整理、ユーザー名をログアウトの上の行に右揃えで表示）。

**アップグレード方法**: `git pull` の後、`sudo ./scripts/update.sh` を実行してください。

---

## English

### v1.0.3 (2026-10-11)

**Bug fixes**
- **Fixed Keycloak login failing with `mismatching_state: CSRF Warning!`**: the session was only marked `permanent` after a successful login, so the cookie holding the pending `state` during the redirect to Keycloak had no explicit expiry. Some browsers would drop that cookie on its own if the tab sat backgrounded for a while, so the first login would succeed but a later login attempt would fail this way.

**To upgrade**: `git pull`, then run `sudo ./scripts/update.sh`.

---

### v1.0.2 (2026-08-29)

**New**
- **Configurable session idle timeout**: login sessions previously had no expiry at all, staying valid until the browser was closed. You can now set how many minutes of inactivity after login it takes for a session to expire automatically (30 minutes by default), from the Config page, with no restart needed. It applies uniformly across Keycloak, local accounts, and per-app OIDC logins alike, and keeps extending as long as activity continues — an idle timeout, not a fixed expiry from login time.

**To upgrade**: `git pull`, then run `sudo ./scripts/update.sh`.

---

### v1.0.1 (2026-08-11)

**New**
- **Bilingual admin console (i18n)**: the console now auto-detects your browser's language and switches between Japanese and English automatically, with a manual toggle at the bottom of the sidebar (saved via a cookie). Every screen is translated.
- **New "Export" section on the Config page**: replaces the "Export all" link that used to sit next to the dashboard's App list. Pick "All" or any individual App/Site/Proxy/Cron Job/Container from a dropdown, then download.

**Security fixes**
- Includes minor security fixes.

**Minor fixes**
- Fixed `install.sh`/`update.sh` not copying the newly-added `i18n.py` module (nodes updated before this release need to re-run `sudo ./scripts/update.sh`).
- Sidebar layout cleanup (dropped the now-redundant `ns:` tag, put the language toggle and version number on one line, right-aligned the username above the logout link).

**To upgrade**: `git pull`, then run `sudo ./scripts/update.sh`.
