# インストールガイド

📖 [English version here](INSTALL_en.md) / 英語版はこちら

このガイドは、**システム全体**（シングルノードのk3s/SpinKubeクラスタと
SpinDeckコンソール）を、何もインストールされていない**Ubuntu 24.04 / 26.04**の
まっさらなホスト（VM、LXCコンテナ、ベアメタルのいずれでも可）に構築する
手順です。このリポジトリの`install.sh`が、以下の全手順を一発で自動化します。
何度実行しても安全です（各ステップは既に完了している作業をスキップするか、
同じ内容を再適用するだけなので、途中で失敗しても`./scripts/install.sh`をもう一度
実行すれば続きから進められます）。

## クイックスタート

```bash
sudo apt-get update && sudo apt-get install -y git
git clone <このリポジトリのURL> spindeck
cd spindeck
sudo ./scripts/install.sh
```

これだけで、クラスタとコンソールが完全に動作する状態になります。
所要時間はネットワーク速度にもよりますが、おおむね10〜20分程度です。

途中、対話的に公開ホスト名とログイン方式を尋ねられます。既定はコンソール
内蔵のローカルアカウント（ユーザー名/パスワード）で、そのままEnterを
押していけばユーザー名`admin`・自動生成パスワードのアカウントが1つ
作られ、インストール完了時に画面に表示されます（パスワードは平文では
どこにも保存されないため、必ず控えてください — 後から`/config`で
変更もできます）。Keycloak等のOIDCログイン基盤を使いたい場合は
プロンプトで`o`を選ぶと、その場でKeycloakの情報を入力できます
（事前にKeycloak側でクライアントを1つ用意しておくとスムーズです。
詳細は下記「Keycloakの準備」を参照）。

### 非対話的インストール

`install.sh`を実行する前に以下の環境変数を設定しておくと、対話的な入力を
スキップできます（スクリプトやCIでの自動プロビジョニングに便利です）。
ローカルアカウント（既定）の場合:

```bash
export SPINDECK_EXTERNAL_HOST=apps.example.com
export SPINDECK_CONSOLE_AUTH_MODE=basic     # 省略時のデフォルトなので実は省略可
export SPINDECK_ADMIN_USERNAME=admin        # 省略時は "admin"
export SPINDECK_ADMIN_PASSWORD=              # 省略すると自動生成（結果は画面に表示）
sudo -E ./scripts/install.sh
```

Keycloak（OIDC）を使う場合:

```bash
export SPINDECK_EXTERNAL_HOST=apps.example.com
export SPINDECK_CONSOLE_AUTH_MODE=oidc
export SPINDECK_OIDC_ISSUER=https://auth.example.com/realms/myrealm
export SPINDECK_OIDC_CLIENT_ID=https://admin.example.com
export SPINDECK_OIDC_CLIENT_SECRET=your-real-secret
export SPINDECK_OIDC_REDIRECT_URI=https://admin.example.com/oidc/callback
sudo -E ./scripts/install.sh
```

（`sudo -E`で、これらの環境変数を`sudo`後にも引き継ぎます。）

### データの置き場所（既定で`/data`）

**新規インストールでは既定で**、k3s本体・ローカルレジストリ・Traefikの
アクセスログ・New Siteのアップロードファイル・containerアプリの永続データは、
`/var/lib/rancher/k3s`・`/var/lib/spinkube-registry`・`/var/log/traefik`・
`/opt/spinapp-ui/sites-data`・`/data/containers`のように散らばる代わりに、
すべて`/data`以下（`/data/k3s`・`/data/registry`・`/data/traefik-logs`・
`/data/sites-data`・`/data/containers`）にまとめられます。別ディスクを
`/data`にマウントしておくホスト向けの構成です。

この既定値を変えたい場合は、`install.sh`実行前に`SPINDECK_DATA_DIR`を
明示的に設定してください（別のパスにする、または空文字を指定して
従来通りの散らばった配置に戻す、のどちらも可能です）:

```bash
export SPINDECK_DATA_DIR=/mnt/bigdisk   # 別の場所にまとめる
# または
export SPINDECK_DATA_DIR=              # 何もまとめず従来の配置のまま
sudo -E ./scripts/install.sh
```

**`/data`既定は新規インストール時にのみ適用されます** — 既にk3sが
インストール済みのホストで`install.sh`を再実行しても、`SPINDECK_DATA_DIR`を
明示的に設定しない限り、データの置き場所は変わりません（安全に
再実行できるスクリプトが、デフォルト値が変わっただけで稼働中のホストの
データを勝手に移動させることがないようにするためです）。既存ホストで
明示的に`SPINDECK_DATA_DIR`を設定して再実行した場合でも、k3s自体の
データ置き場（`--data-dir`）は初回`k3s server`起動時にしか設定できないため
移動しません（レジストリ・Traefikログ・サイトデータ・containerデータの
4つは、既存ホストでも明示的な設定＋`install.sh`の再実行で移動できます）。

> **`/etc/spinapp-ui/env`が既に存在する場合の注意**: `SPINDECK_DATA_DIR`は
> Traefikのアクセスログ置き場（`SPINAPP_UI_TRAEFIK_ACCESS_LOG`）・
> New Siteのアップロード置き場（`SPINAPP_UI_SITES_DATA_DIR`）・containerアプリの
> 永続データ置き場（`SPINAPP_UI_CONTAINER_DATA_DIR`）もコンソール側に伝える
> 必要がありますが、これは`/etc/spinapp-ui/env`が**まだ存在しない**場合にしか
> 自動で書き込まれません（既存の設定ファイルを`install.sh`が勝手に書き換える
> ことはないためです）。既にこのファイルがある状態で（新規インストールで）
> `install.sh`を実行すると、必要な行を手動で追記するよう案内が表示されます。
> この連動を忘れると、`/config`の「Analytics」に一部のアクセスが
> 反映されない、といった分かりにくい症状になります（エラーにはならず、
> 単に古い/存在しないパスを黙って読み続けます）。

### IPv6でのincoming対応（既定でデュアルスタック）

**新規インストールでは既定で**、k3s・Traefik・コンソール自体
（`:8080`直アクセス）がIPv4に加えてIPv6でも待ち受けます（IPv4は
今まで通り動き続けます — IPv6専用にはなりません）。IPv6の発信経路が
無いホストでインストールしても害はありません（該当する案内が
表示されるだけです）。

この既定を無効にしたい場合は、`install.sh`実行前に明示的に空文字を
設定してください:

```bash
export SPINDECK_DUAL_STACK=
sudo -E ./scripts/install.sh
```

**デュアルスタックの既定も新規インストール時にのみ適用されます** —
`SPINDECK_DATA_DIR`と同じ理由で、k3sのcluster-cidr/service-cidrは初回
`k3s server`起動時にしか設定できず、Traefikのネットワーク設定も
稼働中のホストを勝手に変更しないよう、既にk3sがインストール済みの
ホストでは`SPINDECK_DUAL_STACK`を明示的に設定しない限り、これまで通り
IPv4のみのままです。Pod/Service自体のIPv6アドレスは固定のULAレンジ
（`fd42::/56`・`fd43::/112`）を使い、ノード自身が持つ実際のIPv6アドレスへの
発信元変換（NAT）を通じて外部と通信します。egress用の`NetworkPolicy`
（前述の「踏み台」対策）にもIPv6版のブロックルールが自動的に
追加されます。

デュアルスタックで新規インストールしたホストでは、IPv6アドレス
経由のHTTPS(443番)でTraefikへ到達し、デプロイ済みアプリへ正しく
アクセスできます。

### ログイン方式を選ぶ（ローカルアカウント／Keycloak）

コンソールへのログイン方式は`install.sh`実行時のプロンプトで選び、
その後も`/config`の「コンソールのログイン方式」からいつでも切り替え
られます:

- **ローカルアカウント** — 既定の方式です。外部のIDプロバイダを用意
  できない・したくない環境向けに、コンソール自身がユーザー名/パスワードの
  アカウントを複数保持できます。Keycloakの設定は一切不要です。
  `install.sh`のプロンプトでそのままEnterを押すとこちらが選ばれ、
  ユーザー名`admin`・自動生成パスワードの管理者アカウントが1つ作られます
  （インストール完了時の画面に表示されるので必ず控えてください。
  ユーザー名・パスワードとも`/config`から後で変更できます）。
- **Keycloak（OIDC）** — 社内に既にKeycloak（またはOIDC準拠の他の製品）が
  あるなら、`install.sh`のプロンプトで`o`を選んでこちらに切り替えられます。
  事前準備は下記「Keycloakの準備」を参照してください。

インストール後にローカルアカウントとKeycloakを切り替えたい場合、
`/config`の「ローカルアカウント管理」で最低1つアカウントがある状態で
なければ「ローカルアカウント」方式には切り替えられません（誰もログイン
できなくなる状態を防ぐガードです）。`install.sh`で作成した最初の管理者
アカウントを削除する前に、必ず別のアカウントを追加してください。

Keycloakを使う場合は、あらかじめ以下を用意しておいてください:

1. Keycloak管理コンソールで、SpinDeckを動かすレルムに**confidential
   client**（機密クライアント）を1つ作成します。
2. Client IDには、SpinDeckを公開するURL（例:
   `https://admin.example.com`）をそのまま使うのが分かりやすくおすすめ
   です。
3. **Valid Redirect URIs**に `https://admin.example.com/oidc/callback`
   を登録します（末尾の`/oidc/callback`まで含めてください）。新規ノードを
   これから何度も立ち上げる予定なら、下記「新規ノードのブートストラップ」
   のブートストラップ用URIもこの時点で一緒に登録しておくと、ノードを
   立てるたびにKeycloak側をいじらずに済みます。
4. 発行されたClient Secretを控えておきます。

> Client IDにコロン（`:`）が含まれる場合（URLをそのまま使う場合は必ず
> 含まれます）でも問題なく動作するよう、SpinDeck側で対応済みです
> （HTTP Basic認証でクライアント認証すると`user:pass`が最初のコロンで
> 分割されてしまう問題を避けるため、`client_secret_post`方式を強制して
> います）。

### 新規ノードのブートストラップ（OIDC Redirect URIがhttpsの場合）

`SPINAPP_UI_OIDC_REDIRECT_URI`に`https://...`を設定するのが基本ですが、
立ち上げたばかりのノードは、まだ独自ドメイン・証明書・HTTPS化のいずれも
済んでおらず、httpsが疎通していません。この状態でRedirect URIをhttpsに
してしまうと、Keycloakでのログイン自体は成功しても、コールバックの
戻り先（https）に繋がらずログインできない、という鶏と卵の状態になります。

これを避けるため、次の2段階で進めてください:

1. **ブートストラップ**: Keycloakクライアントの Valid Redirect URIs に、
   本番用のhttps URIに加えて`http://<ノードのIP>:8080/oidc/callback`も
   登録しておきます。インストール時は
   `SPINDECK_OIDC_REDIRECT_URI=http://<ノードのIP>:8080/oidc/callback`
   としてこちらを使い、まずはログインできる状態にします。
2. **本番切り替え**: ログインできたら、FQDNの登録・証明書の配置・
   HTTPS化など必要な初期設定を済ませます。落ち着いたら、`/config`の
   「Keycloak (OIDC)」セクションからRedirect URIを本番用の
   `https://...`に変更してください（保存すると自動的にサービスが
   再起動し、以後はhttpsでのログインになります）。

この手順ならKeycloak側は最初に一度2つのURIを登録しておくだけで、ノードを
何台立ち上げてもKeycloakを都度いじる必要はありません。

## スクリプトが行う手順

`install.sh`は以下の15ステップを順番に実行します。途中で失敗しても、
表示されるメッセージに従って再実行すれば大丈夫です。

1. **aptの前提パッケージ** — `curl`、`ca-certificates`、`gnupg`、`git`、
   `cron`、`python3-venv`、`python3-pip`、`openssl`をインストールします。

2. **Node.js 22.x** — NodeSourceリポジトリ経由で導入します（JS→Wasm
   ビルドパイプライン`esbuild`/`jco`に必要です）。

3. **k3s** — `deploy/k3s/k3s-config.yaml`
   （`KubeletInUserNamespace=true` — LXCコンテナで`/dev/kmsg`が無い問題を
   回避するための設定で、他の環境でも無害です）と
   `deploy/k3s/registries.yaml`（ローカルレジストリ`localhost:5000`を
   HTTP非セキュアとして信頼する設定）を使ってk3sをインストールします。
   `SPINDECK_DATA_DIR`を設定していれば、`data-dir: $SPINDECK_DATA_DIR/k3s`
   をこの設定ファイルに追記してからインストールします。
   `SPINDECK_DUAL_STACK`を設定していれば、dual-stackの
   `cluster-cidr`/`service-cidr`/`cluster-dns`もここに追記します。

4. **Spin CLI + containerd-shim-spin-v2** — それぞれのGitHubリリースから
   直接ダウンロードします（このリポジトリの構築・検証時と同じバージョンに
   固定: Spin v4.0.2、シム v0.25.1）。
   `deploy/k3s/containerd-config.toml.tmpl`経由でcontainerdに組み込みます
   （containerd 2.1以降のTransfer Serviceが`wasi/wasm`イメージのpullを
   壊してしまう問題への対処も含みます）。その後k3sを再起動します。

5. **Helm** — 公式の`get-helm-3`スクリプト経由で導入します。

6. **cert-manager v1.20.0** — spin-operatorのwebhook証明書に必要なため
   導入します。

7. **SpinKube** — `SpinApp`/`SpinAppExecutor`のCRD、`wasmtime-spin-v2`の
   RuntimeClass、`containerd-shim-spin`という`SpinAppExecutor`インスタンス
   （`deploy/k8s-manifests/spinkube-crds.yaml`/`spinkube-runtime.yaml`
   — インストール時にfetchするのではなく、実際に動作しているv0.6.1環境
   から抽出したものを使うため、正しいupstreamのリリースアセットURLを
   探す必要がありません）、そして`spin-operator` v0.6.1本体をHelmチャート
   経由で導入します。

8. **ローカルOCIレジストリ** — `hostNetwork: true`の単純な`registry:2`
   Deploymentを導入します。`REGISTRY_HTTP_ADDR`により`127.0.0.1:5000`
   限定でbindされているため、ノード自身のcontainerdからは到達できますが、
   Podやより広いネットワークからは到達できません。認証・TLSはありません
   （本当に`localhost`専用のため問題ありません）。イメージの保存先は
   `deploy/k8s-manifests/registry.yaml.tmpl`から、デフォルトでは
   `/var/lib/spinkube-registry`、`SPINDECK_DATA_DIR`を設定していれば
   `$SPINDECK_DATA_DIR/registry`に置き換えて適用します。

9. **egress用`NetworkPolicy`** — デプロイされる全てのSpinApp/CronJobの
   Podの外向き通信を、パブリックインターネットと自分のLANまでに制限
   しつつ、このクラスタ自身の内部（k8s APIサーバー、他のClusterIP
   サービス）やクラウドのメタデータエンドポイントへの到達は遮断します。
   `deploy/k8s-manifests/spinapp-egress-policy.yaml.tmpl`を元に、
   `__NODE_IP__`を（`ip -4 route get 1.1.1.1`で自動検出した）このノード
   自身のLAN IPに置き換えてから適用します。なぜこれが必要か、なぜこの
   範囲になっているかは、そのファイル自身のコメントを参照してください。
   「セルフサービスでコードを実行できる」という機能上どうしても生まれる
   「このシステム自体が踏み台になる」リスクを塞ぐためのものです。
   `SPINDECK_DUAL_STACK`を設定していれば、IPv6版のブロックルールも
   同様に追加します（未設定時、またはこのノードのIPv6が自動検出できない
   場合は、IPv6のルールごと省略されます）。

10. **Traefik** — JSONアクセスログを有効化し、Pod CIDRからの
    `X-Forwarded-For`を信頼するよう設定し（k3s内蔵のロードバランサーが
    接続をSNATするため必要です）、アクセスログを**永続的な**hostPathの
    ファイル（デフォルトでは`/var/log/traefik/access.log`、
    `SPINDECK_DATA_DIR`を設定していれば
    `$SPINDECK_DATA_DIR/traefik-logs/access.log`、Traefikのコンテナが
    非rootで動くためUID 65532所有）に書き出すことで、Traefik Podが
    再起動してもログ履歴が残るようにします。`SPINDECK_DUAL_STACK`を
    設定していれば、TraefikのServiceを`ipFamilyPolicy: PreferDualStack`
    にしてIPv6でも待ち受けるようにします。

11. **SpinDeckコンソール本体** — `/opt/spinapp-ui/venv`にPython venvを
    作成し、`app.py`とテンプレート一式を`/opt/spinapp-ui/`にコピーし、
    JSビルドテンプレートを`/opt/spinapp-ui/js-template-src/hello-js/`に
    コピーして一度だけ`npm install`を実行します（コンソールはこれ以降、
    アプリをデプロイするたびにこのディレクトリをハードリンクコピーして
    使うため、`npm install`が必要なのはこの一度だけです）。

12. **コンソール用の最小権限kubeconfig** — `deploy/k8s-manifests/spinapp-ui-rbac.yaml`
    （専用の`ServiceAccount`+`Role`+`RoleBinding`、`default`名前空間内で
    コンソールが実際に使うリソース種別だけに絞った権限）を適用し、
    そのServiceAccountのトークンから`/etc/spinapp-ui/kubeconfig.yaml`
    （権限600）を生成します。コンソール自体はこれ以降、`/etc/rancher/k3s/k3s.yaml`
    （クラスタ全体のadmin権限）ではなく、こちらの最小権限kubeconfigで
    動作します。再実行しても同じ内容になるだけなので安全です。

13. **設定ファイルの作成** — `/etc/spinapp-ui/env`を書き出します
    （対話的な入力、または`SPINDECK_*`環境変数、あるいは後で編集できる
    プレースホルダーから）。このファイルが既に存在する場合は完全に
    スキップされるため、スクリプトを再実行しても既に設定済みの本物の
    シークレットが上書きされることはありません（ステップ12で新しく生成
    された最小権限kubeconfigへの切り替えも、既存の環境ファイルには
    自動反映されません — 案内メッセージに従って手動で切り替えてください）。
    `SPINDECK_DATA_DIR`を設定していれば、New Siteのアップロードファイルの
    保存先として`SPINAPP_UI_SITES_DATA_DIR=$SPINDECK_DATA_DIR/sites-data`も
    書き込みます。

14. **systemdユニット** — `spinapp-ui`サービスをインストールし起動します。

15. **スタックしたWasmシムの自動復旧タイマー** — `deploy/spin-shim-recovery.sh`を
    `/opt/spinapp-ui/`に配置し、`spin-shim-recovery.timer`（15分おき）を
    有効化します。依存している`containerd-shim-spin`（外部プロジェクト）の
    既知の不具合（コンテナ起動失敗時にプロセスが残り続け、それ以降の新規
    Wasmコンテナ起動まで巻き添えでブロックする）を自動検知・復旧するための
    安全装置です。詳細は`deploy/spin-shim-recovery.sh`自身のコメントを
    参照してください。

## インストール後にすること

- `http://<サーバーのIP>:8080/`を開いてください — 既定（ローカルアカウント）
  なら、インストール完了時に画面表示されたユーザー名/パスワードでログイン
  できます。Keycloakを選んだ場合はKeycloakへのログインにリダイレクトされます
  （プレースホルダーの値のままだとOIDCエラーになります — その場合は
  `/etc/spinapp-ui/env`を編集して`systemctl restart spinapp-ui`してください）。
- ログイン後、`/config`からドメインを登録してください — 最初に登録した
  ものがダッシュボード/プレビューに表示されるデフォルトのドメインになり、
  各アプリは任意でそのいずれかにピン留めできます。
- 何かおかしい場合は、まず以下のコマンドで状態を確認してください:
  - `systemctl status spinapp-ui` — コンソール自体が動いているか
  - `kubectl get nodes` — k3sクラスタ自体が正常か
  - `kubectl get pods -A` — 各コンポーネント（Traefik、cert-manager、
    spin-operatorなど）のPodが全て`Running`になっているか
  - `journalctl -u spinapp-ui -f` — コンソールのログをリアルタイムで確認
- SpinDeck自体の使い方（アプリの作り方、認証の設定方法など）が初めての方は、
  [`docs/GUIDE.md`](GUIDE.md)を参照してください。実際に何をどう
  クリックすればよいかを、初心者向けにまとめています。

## アンインストール

```bash
systemctl disable --now spinapp-ui
rm -rf /opt/spinapp-ui /etc/spinapp-ui /etc/systemd/system/spinapp-ui.service
/usr/local/bin/k3s-uninstall.sh   # クラスタ全体を破棄（設定したdata-dirも含む）
```

`spinapp-ui`サービスを止めて関連ディレクトリを削除し、
`k3s-uninstall.sh`（k3sが自動生成するアンインストールスクリプト）で
クラスタ全体を破棄します。デプロイしたアプリのデータ（`/opt/spinapp-ui`
以下や、レジストリのイメージ等）も全て失われるため、必要なものは事前に
バックアップしてください。

`SPINDECK_DATA_DIR`を設定していた場合（新規インストールでは既定で
`/data`）、k3s本体のデータは`k3s-uninstall.sh`が設定済みの`data-dir`から
削除しますが、レジストリ・Traefikログ・New Siteのファイル・container
アプリの永続データ（`$SPINDECK_DATA_DIR/{registry,traefik-logs,sites-data,
containers}`）はk3sの管理下ではないただのディレクトリのため、
`k3s-uninstall.sh`では消えません。不要であれば`rm -rf`で個別に削除して
ください。
