"""設定ファイルの読み込みとチェック。

設定・状態・キャッシュは、既定ではすべてこのリポジトリ（clone したディレクトリ）の下に置く。
  記録する側（監視対象）: config/record.yaml   （例は docs/record.sample.yaml）  状態: data/record/
  調べる側              : config/review.yaml   （例は docs/review.sample.yaml）  キャッシュ: data/cache/
設定ファイルの中の相対パスは、その設定ファイルのあるディレクトリが基準。
"""

import copy
import os

import yaml

from fhashes import patterns, snapshot

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RECORD_CONFIG = os.path.join(PROJECT_DIR, "config", "record.yaml")
DEFAULT_REVIEW_CONFIG = os.path.join(PROJECT_DIR, "config", "review.yaml")

RECORD_DEFAULTS = {
    "host": None,                  # 必須。スナップショットに書くホスト名
    "state_dir": "../data/record",  # 状態ファイルと送信待ちのスナップショットの置き場所
    "include": [],
    "exclude": [],
    "reread_cycle": 24,            # この回数の実行で、すべてのファイルを一度は読み直す
    "upload": {
        "remote": None,            # 送り先（例: "gcs-fhashes:fhashes-snapshots/web1"）。この直下に置く。null なら送信しない
        "rclone_config": None,     # rclone の設定ファイル。null なら rclone の既定の場所
        "rclone_options": [],      # rclone に渡す追加のオプション（ふつうは不要）
        "rclone": "rclone",        # rclone のコマンドのパス
    },
}

REVIEW_DEFAULTS = {
    "cache_dir": "../data/cache",  # ダウンロードしたスナップショットの置き場所。相対パスはこの設定ファイルのあるディレクトリが基準
    "rclone_config": None,
    "rclone_options": [],
    "rclone": "rclone",
    "hosts": None,  # 必須。調べるホストと、そのスナップショットを置いているストレージ（record の upload.remote と同じ値）
}


class ConfigError(Exception):
    pass


def read_yaml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(path + ": 設定ファイルがありません")
    except yaml.YAMLError as e:
        raise ConfigError(path + ": YAML の書き方が不正です: " + str(e))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(path + ": 先頭は「キー: 値」の形で書いてください")
    return data


def merge_defaults(path: str, data: dict, defaults: dict) -> dict:
    """既定値に設定値を上書きした dict を返す。知らないキーがあればエラー（書き間違い対策）。"""
    result = copy.deepcopy(defaults)
    for key, value in data.items():
        if key not in defaults:
            raise ConfigError(path + ": 不明な設定項目です: " + str(key))
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(path + ": " + key + " は「キー: 値」の形で書いてください")
            for sub_key in value:
                if sub_key not in defaults[key]:
                    raise ConfigError(path + ": 不明な設定項目です: " + key + "." + str(sub_key))
            result[key].update(value)
        else:
            result[key] = value
    return result


def resolve_path(config_path: str, value: str) -> str:
    """設定ファイルに書かれたパスを絶対パスにする。相対パスは設定ファイルのあるディレクトリが基準。"""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config_path)), value))


def check_string_list(path: str, name: str, value) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(path + ": " + name + " は文字列のリスト（- で始まる行）で書いてください")


def check_rclone_settings(path: str, prefix: str, section: dict) -> None:
    """rclone / rclone_config / rclone_options を確かめる。rclone_config は絶対パスにする。"""
    if not isinstance(section["rclone"], str) or not section["rclone"]:
        raise ConfigError(path + ": " + prefix + "rclone は rclone のコマンドのパス（文字列）で書いてください")
    if section["rclone_config"] is not None:
        if not isinstance(section["rclone_config"], str) or not section["rclone_config"]:
            raise ConfigError(path + ": " + prefix + "rclone_config は rclone の設定ファイルのパスで書いてください")
        section["rclone_config"] = resolve_path(path, section["rclone_config"])
    check_string_list(path, prefix + "rclone_options", section["rclone_options"])


# ----------------------------------------------------------------------
# 記録する側
# ----------------------------------------------------------------------

def load_record_config(path: str) -> dict:
    conf = merge_defaults(path, read_yaml(path), RECORD_DEFAULTS)
    conf["state_dir"] = resolve_path(path, conf["state_dir"])
    if not conf["host"]:
        # 自動でホスト名を使うと、ホスト名の変更や重複に気付きにくいので、必ず書いてもらう
        raise ConfigError(path + ": host を書いてください（スナップショットに書くホスト名）")
    if not isinstance(conf["host"], str) or not snapshot.HOST_NAME_REGEX.match(conf["host"]):
        raise ConfigError(path + ": host に使えない文字があります: " + conf["host"])

    for key in ("include", "exclude"):
        check_string_list(path, key, conf[key])
        for pattern in conf[key]:
            try:
                patterns.check_pattern(pattern)
            except ValueError as e:
                raise ConfigError(path + ": " + key + ": " + str(e))
    if not conf["include"]:
        raise ConfigError(path + ": include が空です")

    if not isinstance(conf["reread_cycle"], int) or conf["reread_cycle"] < 1:
        raise ConfigError(path + ": reread_cycle は 1 以上の整数にしてください")
    check_rclone_settings(path, "upload.", conf["upload"])
    conf["config_path"] = path
    return conf


# ----------------------------------------------------------------------
# 調べる側
# ----------------------------------------------------------------------

def load_review_config(path: str) -> dict:
    conf = merge_defaults(path, read_yaml(path), REVIEW_DEFAULTS)
    conf["cache_dir"] = resolve_path(path, conf["cache_dir"])
    check_rclone_settings(path, "", conf)

    if not isinstance(conf["hosts"], dict) or not conf["hosts"]:
        raise ConfigError(path + ": hosts に、調べるホストとストレージを 1 つ以上書いてください"
                                 "（例: web1: \"gcs-fhashes-ro:fhashes-snapshots\"）")
    for host, remote in conf["hosts"].items():
        if not isinstance(host, str) or not snapshot.HOST_NAME_REGEX.match(host):
            raise ConfigError(path + ": hosts のホスト名が不正です: " + str(host))
        if not isinstance(remote, str) or not remote:
            raise ConfigError(path + ": hosts の " + host + " にストレージ（rclone の場所）を書いてください")
    conf["config_path"] = path
    return conf
