"""rclone の決まりごと（記録する側と調べる側の両方で使う）。

rclone の実行そのものは、失敗したときの扱いが用途ごとに違うので、それぞれの側で行う
（送信はファイルごとに続行、取得は中止）。
"""


def base_command(settings: dict) -> list:
    """rclone のコマンドの先頭部分（コマンド、--config、追加のオプション）。

    settings は rclone / rclone_config / rclone_options を持つ設定（record の upload、または review 全体）。
    """
    command = [settings["rclone"]]
    if settings["rclone_config"]:
        command += ["--config", settings["rclone_config"]]
    return command + list(settings["rclone_options"])


def join_remote(remote: str, path: str) -> str:
    """"gcs:bucket/prefix" と "host/2026/..." をつなぐ。remote が "/" や ":" で終わる場合も考える。"""
    if remote.endswith("/") or remote.endswith(":"):
        return remote + path
    return remote + "/" + path
