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
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "server.log"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_file_logging(data_dir: Path) -> Path:
    """ルートロガーにファイル＋標準エラーのハンドラを設定し、ログパスを返す。

    再呼び出しは同じファイルへのハンドラを重複追加しない（冪等）。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = (data_dir / LOG_FILE_NAME).resolve()
    root = logging.getLogger()
    root.setLevel(logging.INFO)

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
