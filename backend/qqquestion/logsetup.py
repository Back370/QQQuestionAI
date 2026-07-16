"""バックエンドのファイルログ設定。

VSCode 拡張経由で起動するとバックエンドの標準出力は拡張の出力チャンネルに
しか流れず、ウィンドウを閉じると消えてしまう。生成失敗（APIのレート制限等）の
原因を後から特定できるよう、ルートロガーにローテーション付きファイルハンドラを
取り付け、`QQQ_DATA_DIR/server.log` に永続化する。

uvicorn は log_config=None で起動する（server.main 参照）。uvicorn 既定の
ログ設定は uvicorn.* ロガーの propagate を切ってしまい、ここで設定した
ルートのハンドラにアクセスログ・例外ログが届かなくなるため。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "server.log"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# 拡張が常時ポーリングするエンドポイント。成功時のアクセスログを残すと、
# VSCode を開いているだけで毎時 600KB 積み上がり、ローテーション（1MB×3世代）が
# 数時間で一周して、肝心の生成失敗のログを押し流してしまう。
_POLLING_PATHS = ("/health", "/quiz/pending")


class _DropSuccessfulPollingAccessLogs(logging.Filter):
    """ポーリングの成功アクセスログだけを落とす。

    uvicorn のアクセスログは record.args が
    (client_addr, method, full_path, http_version, status_code) の5要素。
    整形済み文字列ではなくこの args で判定する（フォーマット変更に強い）。
    落とすのは 2xx のみ。エラー応答は原因調査に必要なので必ず残す。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, full_path, _http_version, status = args
        if method != "GET" or not isinstance(status, int) or not 200 <= status < 300:
            return True
        path = str(full_path).split("?", 1)[0]
        return path not in _POLLING_PATHS


def setup_file_logging(data_dir: Path) -> Path:
    """ルートロガーにファイル＋標準エラーのハンドラを設定し、ログパスを返す。

    再呼び出しは同じファイルへのハンドラを重複追加しない（冪等）。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = (data_dir / LOG_FILE_NAME).resolve()
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # ポーリングのノイズ抑制。QQQ_LOG_POLLING=1 で全リクエストを残せる
    # （「拡張がポーリングできているか」自体を調べたいときのため）
    access = logging.getLogger("uvicorn.access")
    installed = [
        f for f in access.filters if isinstance(f, _DropSuccessfulPollingAccessLogs)
    ]
    if os.environ.get("QQQ_LOG_POLLING") == "1":
        for f in installed:
            access.removeFilter(f)
    elif not installed:
        access.addFilter(_DropSuccessfulPollingAccessLogs())

    already = any(
        isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_path
        for h in root.handlers
    )
    if not already:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(file_handler)

    # 従来どおり拡張の出力チャンネル（stderr）にも流す
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in root.handlers
    ):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream_handler)

    return log_path
