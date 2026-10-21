# ストレージの設定: AWS S3

README の「ストレージ」の具体的な手順です。例では、バケット名を `fhashes-snapshots`、保持期間を 180 日、削除を 181 日後とします。以下のコマンドは手順の目安です。**実際の環境で、送信と一覧・取得ができることを必ず確かめてください。**

## バケット

```
# バケット（オブジェクトロックは作成時にしか有効にできない）
aws s3api create-bucket --bucket fhashes-snapshots --region ap-northeast-1 \
  --create-bucket-configuration LocationConstraint=ap-northeast-1 --object-lock-enabled-for-bucket

# 消せなくする（GOVERNANCE は特別な権限があれば解除できる。本番では COMPLIANCE にすると誰にも消せない）
aws s3api put-object-lock-configuration --bucket fhashes-snapshots --object-lock-configuration \
  '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Days":180}}}'

# 期限後に消す
aws s3api put-bucket-lifecycle-configuration --bucket fhashes-snapshots --lifecycle-configuration \
  '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":181},"NoncurrentVersionExpiration":{"NoncurrentDays":1}}]}'
```

## 記録する側

IAM ポリシー（作成と読み取り、一覧）:

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::fhashes-snapshots"},
  {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": "arn:aws:s3:::fhashes-snapshots/*"}
]}
```

- 一覧（`s3:ListBucket`）も必要です。rclone は送る前に送り先に同じ名前のものがあるかを確かめますが、S3 は一覧の権限がない鍵に対しては、ないものを「ない（404）」ではなく「拒否（403）」と答えるため、送信が失敗します。
- S3 では、同じ名前で違う中身を送ると、拒否されずに新しいバージョンとして保存されます（前のバージョンはオブジェクトロックで残ります）。調べる側は最新のバージョンを読むので、置き換えられた場合はハッシュチェーンの異常として見つかります。

rclone の設定と `config/record.yaml`:

```
rclone --config config/rclone.conf config create fhashes-s3 s3 provider AWS region ap-northeast-1 \
  access_key_id <アクセスキー> secret_access_key <シークレットキー>
```

```yaml
upload:
  remote: "fhashes-s3:fhashes-snapshots/web1"
  rclone_config: rclone.conf
```

## 調べる側

IAM ポリシー（読み取りと一覧）:

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::fhashes-snapshots"},
  {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::fhashes-snapshots/*"}
]}
```

rclone の設定と `config/review.yaml`:

```
rclone --config config/rclone.conf config create fhashes-s3-ro s3 provider AWS region ap-northeast-1 \
  access_key_id <アクセスキー> secret_access_key <シークレットキー>
```

```yaml
rclone_config: rclone.conf
hosts:
  web1: "fhashes-s3-ro:fhashes-snapshots/web1"
```

## より安全にする場合（任意）

記録する側の権限を、そのホストの場所だけに絞ります（IAM ポリシーの `s3:PutObject`・`s3:GetObject` の `Resource` を `arn:aws:s3:::fhashes-snapshots/web1/*` にし、`s3:ListBucket` には `"Condition": {"StringLike": {"s3:prefix": ["web1/*"]}}` を付ける）。一覧を絞った状態でも送信できることを、実際の環境で確かめてください。または、ホストごとにバケットを分けます。
