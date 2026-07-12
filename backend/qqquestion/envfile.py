"""実行時に .env ファイルを環境変数へ読み込む。

GOOGLE_API_KEY / TAVILY_API_KEY 等を毎回シェルで export しなくて済むよう、
サーバ・CLI の起動時に backend/.env（または QQQ_ENV_FILE で指定した
ファイル）を読む。秘密情報を扱うため、値をログ・例外メッセージに
含めないこと。既に設定済みの環境変数は上書きしない。
"""

from __future__ import annotations

import os
from pathlib import Path

_QUOTES = ('"', "'")


def load_env_file(path: str | Path | None = None) -> list[str]:
    """KEY=VALUE 形式の .env を os.environ に反映し、設定したキー名を返す。

    - ファイルが無ければ何もしない（.env は任意）
    - `#` 始まりの行と空行は無視、`export KEY=VALUE` 形式も許容
    - 既存の環境変数は上書きしない（シェルでの明示指定を優先）
    """
    env_path = Path(path or os.environ.get("QQQ_ENV_FILE", ".env"))
    if not env_path.is_file():
        return []

    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)  # キー名のみ。値は決して記録しない
    return loaded
