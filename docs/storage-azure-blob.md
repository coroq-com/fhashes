# ストレージの設定: Azure Blob Storage

README の「ストレージ」の具体的な手順です。例では、ストレージアカウントを `fhashesstore`、コンテナを `fhashes-snapshots`、保持期間を 180 日、削除を 181 日後とします。以下のコマンドは手順の目安です。**実際の環境で、送信と一覧・取得ができることを必ず確かめてください。**

## ストレージアカウントとコンテナ

```
# ストレージアカウントとコンテナ
az storage account create -n fhashesstore -g <リソースグループ> -l japaneast --sku Standard_LRS
az storage container create --account-name fhashesstore -n fhashes-snapshots

# 消せなくする（ロックは試した後で。ロックすると取り消せない）
az storage container immutability-policy create -g <リソースグループ> --account-name fhashesstore \
  --container-name fhashes-snapshots --period 180
# az storage container immutability-policy lock -g <リソースグループ> --account-name fhashesstore \
#   --container-name fhashes-snapshots --if-match <etag>

# 期限後に消す
echo '{"rules":[{"enabled":true,"name":"expire","type":"Lifecycle","definition":{"actions":{"baseBlob":{"delete":{"daysAfterModificationGreaterThan":181}}},"filters":{"blobTypes":["blockBlob"]}}}]}' > policy.json
az storage account management-policy create -g <リソースグループ> --account-name fhashesstore --policy @policy.json

# 記録する側の SAS（読み取り・作成・書き込み）と、調べる側の SAS（読み取り・一覧）
az storage container generate-sas --account-name fhashesstore -n fhashes-snapshots \
  --permissions rcw --expiry 2027-12-31 --https-only
az storage container generate-sas --account-name fhashesstore -n fhashes-snapshots \
  --permissions rl --expiry 2027-12-31 --https-only
```

- SAS には有効期限があります。期限が切れる前に作り直して、rclone の設定を更新してください。
- 不変ストレージの保持期間中は、書き込み（`w`）の権限があっても、既存のものの上書きや削除はできません。

## 記録する側

```
rclone --config config/rclone.conf config create fhashes-azure azureblob \
  sas_url "https://fhashesstore.blob.core.windows.net/fhashes-snapshots?<記録する側の SAS>"
```

`config/record.yaml`:

```yaml
upload:
  remote: "fhashes-azure:fhashes-snapshots/web1"
  rclone_config: rclone.conf
```

## 調べる側

```
rclone --config config/rclone.conf config create fhashes-azure-ro azureblob \
  sas_url "https://fhashesstore.blob.core.windows.net/fhashes-snapshots?<調べる側の SAS>"
```

`config/review.yaml`:

```yaml
rclone_config: rclone.conf
hosts:
  web1: "fhashes-azure-ro:fhashes-snapshots/web1"
```

## より安全にする場合（任意）

SAS はコンテナ単位なので、場所（パス）では絞れません。ホストごとにコンテナを分け、SAS もコンテナごとに作ります。
