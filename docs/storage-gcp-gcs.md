# ストレージの設定: Google Cloud Storage

README の「ストレージ」の具体的な手順です。例では、バケット名を `fhashes-snapshots`、保持期間を 180 日、削除を 181 日後とします。以下のコマンドは手順の目安です。**実際の環境で、送信と一覧・取得ができることを必ず確かめてください。**

## サービスアカウントとバケット

```
# サービスアカウント（記録する側はホストごと、調べる側は 1 つ）
gcloud iam service-accounts create fhashes-web1 --display-name="fhashes record web1" --project=<プロジェクト>
gcloud iam service-accounts create fhashes-review --display-name="fhashes review" --project=<プロジェクト>

# バケット
gcloud storage buckets create gs://fhashes-snapshots --location=asia-northeast1 --uniform-bucket-level-access

# 消せなくする（ロックは試した後で。ロックすると取り消せない）
gcloud storage buckets update gs://fhashes-snapshots --retention-period=180d
# gcloud storage buckets update gs://fhashes-snapshots --lock-retention-period

# 期限後に消す
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":181}}]}' > lifecycle.json
gcloud storage buckets update gs://fhashes-snapshots --lifecycle-file=lifecycle.json

# 記録する側（ホストごとのサービスアカウント）: 作成と読み取り
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  gcloud storage buckets add-iam-policy-binding gs://fhashes-snapshots \
    --member=serviceAccount:fhashes-web1@<プロジェクト>.iam.gserviceaccount.com --role=$role
done

# 調べる側: 読み取りと一覧
gcloud storage buckets add-iam-policy-binding gs://fhashes-snapshots \
  --member=serviceAccount:fhashes-review@<プロジェクト>.iam.gserviceaccount.com --role=roles/storage.objectViewer
```

- `roles/storage.objectCreator` は作成だけの権限で、上書き・削除はできません（上書きには削除の権限が要るため）。

## rclone の設定

監視対象の置き場所によって変わります。

- **監視対象が GCP の VM の場合**: 鍵ファイルは不要です。VM にサービスアカウント `fhashes-web1` を割り当て、rclone は VM から認証情報を取得します（VM に割り当てられるサービスアカウントは 1 つだけです。ほかの用途で使っている場合は、次の鍵ファイルの方法にします）。
  ```
  rclone --config config/rclone.conf config create fhashes-gcs gcs env_auth true bucket_policy_only true
  ```
- **監視対象が GCP の外（AWS・Azure・オンプレミスなど）の場合**: 鍵ファイル（JSON）を作って監視対象に置き、監視に使うユーザーだけが読めるようにします。`config/` の下に置けば git の管理外になります。
  ```
  gcloud iam service-accounts keys create fhashes-web1.json \
    --iam-account=fhashes-web1@<プロジェクト>.iam.gserviceaccount.com
  # 監視対象で
  mv fhashes-web1.json config/ && chmod 600 config/fhashes-web1.json
  rclone --config config/rclone.conf config create fhashes-gcs gcs \
    service_account_file /path/to/fhashes/config/fhashes-web1.json bucket_policy_only true
  ```
  組織のポリシーで鍵の作成が禁止されている場合があります（`iam.disableServiceAccountKeyCreation`）。その場合は、組織の管理者に例外を設定してもらうか、Workload Identity 連携（鍵ファイルを使わない方法）を使います。

調べる側も、同じようにサービスアカウント `fhashes-review` で rclone を設定します（例: `fhashes-gcs-ro`）。

`config/record.yaml`:

```yaml
upload:
  remote: "fhashes-gcs:fhashes-snapshots/web1"
  rclone_config: rclone.conf
```

`config/review.yaml`:

```yaml
rclone_config: rclone.conf
hosts:
  web1: "fhashes-gcs-ro:fhashes-snapshots/web1"
```

## より安全にする場合（任意）

記録する側の権限を、そのホストの場所だけに絞ります。IAM の条件を付けて、2 つのロールを与えます。

```
--condition='expression=resource.name.startsWith("projects/_/buckets/fhashes-snapshots/objects/web1/"),title=web1-only'
```

または、ホストごとにバケットを分けます。
