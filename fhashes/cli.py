"""コマンドラインの入口。使い方は `fhashes --help` / `fhashes <コマンド> --help` を参照。

記録する側（監視対象で動かす）: record                   設定: config/record.yaml
調べる側                      : review / status / clean  設定: config/review.yaml
（config/ はこのリポジトリの中。--config か環境変数で変えられる）
"""

import argparse
import logging
import os
import sys

from fhashes import config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fhashes", description="ファイル改ざんの記録と調査")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳しいログを出す")
    sub = parser.add_subparsers(dest="command", required=True)

    record_help = "設定ファイル（既定: 環境変数 FHASHES_RECORD_CONFIG か <リポジトリ>/config/record.yaml）"
    review_help = "設定ファイル（既定: 環境変数 FHASHES_REVIEW_CONFIG か <リポジトリ>/config/review.yaml）"

    # 記録する側
    p = sub.add_parser("record", help="[記録] ファイルを調べて記録を作り、送信待ちの記録と一緒にストレージへ送る")
    p.add_argument("--config", help=record_help)
    p.add_argument("--no-upload", action="store_true", help="記録を作るだけで送らない")

    # 調べる側
    p = sub.add_parser("status", help="[調査] ホストごとの最新の記録と異常を表示する")
    p.add_argument("--config", help=review_help)

    p = sub.add_parser("review", help="[調査] 1 台のホストについて、期間内の変化と監視の状況を見直す")
    p.add_argument("--config", help=review_help)
    p.add_argument("host", help="ホスト名（1 台だけ）")
    p.add_argument("--from", dest="since",
                   help="開始日時（例: 2026-09-20、'2026-09-20 10:00'、2026-09-20T10:00+09:00、2026-09-20T01:00Z）。"
                        "タイムゾーンを書かなければ実行環境のタイムゾーン（TZ）で解釈する。"
                        "省くと最初の記録から（全期間はダウンロードの量が多くなる）")
    p.add_argument("--to", dest="until",
                   help="終了日時（形式は --from と同じ。日付だけならその日を含む）。省くと最新の記録まで")
    p.add_argument("--detected", action="store_true",
                   help="変化を「指定期間に検知したもの」で絞り込む（既定は「変更された可能性のある期間が重なるもの」）")
    p.add_argument("--path", action="append",
                   help="変化をパスのワイルドカードで絞り込む（例: '/etc/**'、'*.php'）。複数指定するといずれかに一致するもの")
    p.add_argument("--type", help="変化を種別で絞り込む（added,modified,deleted をカンマ区切りで）")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table",
                   help="出力形式（csv / json は変化だけを、時刻を UTC で出す）")

    p = sub.add_parser("clean", help="[調査] ダウンロードした記録のキャッシュを消す")
    p.add_argument("--config", help=review_help)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        if args.command == "record":
            return record_command(args)
        return review_command(args)
    except config.ConfigError as e:
        logging.error("設定エラー: %s", e)
        return 2
    except ValueError as e:
        logging.error("%s", e)
        return 2
    except BrokenPipeError:
        return 0  # `fhashes review ... --format csv | head` で途中で閉じられた場合


def record_command(args) -> int:
    from fhashes import walker
    from fhashes.record import recorder, upload

    path = args.config or os.environ.get("FHASHES_RECORD_CONFIG") or config.DEFAULT_RECORD_CONFIG
    conf = config.load_record_config(path)

    recorded = True
    try:
        recorder.record(conf)
    except recorder.Skipped:
        logging.info("ほかの fhashes record が実行中なので、今回は何もしません")
        return 0
    except walker.MissingRootError as e:
        logging.error("記録に失敗しました（記録は作っていません）: %s", e)
        recorded = False
    if args.no_upload:
        return 0 if recorded else 1
    # 今回の記録に失敗しても、前に送れなかったスナップショットは送る
    failed = upload.upload_outbox(conf)
    return 1 if failed or not recorded else 0


def review_command(args) -> int:
    from fhashes.review import report, storage

    path = args.config or os.environ.get("FHASHES_REVIEW_CONFIG") or config.DEFAULT_REVIEW_CONFIG
    conf = config.load_review_config(path)
    try:
        if args.command == "status":
            return report.cmd_status(conf, args)
        if args.command == "review":
            return report.cmd_review(conf, args)
        if args.command == "clean":
            return report.cmd_clean(conf, args)
    except storage.StorageError as e:
        logging.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
