"""送信: outbox/ のスナップショットを rclone でストレージへ送る。送れたものはローカルから消す。

ファイルごとに `rclone copyto` を実行し、成功したらローカルのファイルを消す。
1 つ失敗してもほかのファイルの送信は続ける。失敗したものは outbox/ に残り、次回また送る。
"""

import logging
import os
import subprocess
import time

from fhashes import rclone

log = logging.getLogger("fhashes")

RCLONE_TIMEOUT = 600          # 1 ファイルあたりの上限（秒）
STUCK_WARNING_SECONDS = 3 * 3600  # これより古いファイルが残っていたら警告する


def outbox_files(state_dir: str) -> list:
    """outbox/ の中のスナップショットファイルを、古い順（名前順）に返す。(ローカルのパス, ファイル名)"""
    outbox = os.path.join(state_dir, "outbox")
    if not os.path.isdir(outbox):
        return []
    return [(os.path.join(outbox, name), name) for name in sorted(os.listdir(outbox))]


# 送信のときに常に付けるオプション
#   --checksum : 送り先に同じ名前・同じ中身（MD5）のものがあれば、送らずに成功とする。
#                送信は成功したのに失敗と報告された回の送り直しを、送信済みとして片付けるため
#                （WORM のストレージは上書きを拒否するので、これがないと失敗し続ける）
UPLOAD_OPTIONS = ["--checksum"]

# 送信のときに設定する環境変数: バケットの有無を確かめない（記録する側の鍵にはその権限がない）。
# ほかのクラウドの設定は無視されるので、どのクラウドでも全部設定してよい。
# オプション（--gcs-no-check-bucket など）ではなく環境変数で渡すのは、古い rclone（Debian 12 などの 1.60）が
# 知らないオプションはエラーにするが、知らない環境変数は無視するため
UPLOAD_ENV = {
    "RCLONE_S3_NO_CHECK_BUCKET": "true",            # rclone 1.53 以降
    "RCLONE_GCS_NO_CHECK_BUCKET": "true",           # rclone 1.59 以降
    "RCLONE_AZUREBLOB_NO_CHECK_CONTAINER": "true",  # rclone 1.61 以降
}


def rclone_command(upload_conf: dict, local_path: str, destination: str) -> list:
    return rclone.base_command(upload_conf) + ["copyto"] + UPLOAD_OPTIONS + [local_path, destination]



def upload_outbox(conf: dict) -> int:
    """outbox/ のスナップショットをすべて送る。送れなかった件数を返す。"""
    upload_conf = conf["upload"]
    if not upload_conf.get("remote"):
        log.info("upload.remote が設定されていないので、送信しません")
        return 0

    failed = 0
    for local_path, name in outbox_files(conf["state_dir"]):
        destination = rclone.join_remote(upload_conf["remote"], name)
        command = rclone_command(upload_conf, local_path, destination)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=RCLONE_TIMEOUT, env=dict(os.environ, **UPLOAD_ENV))
            output = result.stderr.decode("utf-8", "replace").strip()
            ok = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            output = type(e).__name__ + ": " + str(e)
            ok = False

        if ok:
            os.remove(local_path)
            log.info("送信しました: %s", destination)
        else:
            failed += 1
            log.error("送信に失敗しました: %s: %s", name, output[-1000:])
            warn_if_stuck(local_path, name)

    return failed


def warn_if_stuck(local_path: str, name: str) -> None:
    age = time.time() - os.path.getmtime(local_path)
    if age > STUCK_WARNING_SECONDS:
        log.warning("送信できないまま %.1f 時間たっています: %s（送り先に同じ名前のものがあって上書きできない場合は、"
                    "中身が違うか、記録する側に送り先の読み取りの権限がありません。README の「ストレージ」を参照）",
                    age / 3600, name)
