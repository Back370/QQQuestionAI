"""ターミナル入力（input()）の日本語編集を正しく扱うためのヘルパー。

macOS/BSD の tty はカノニカルモードのバックスペースを **1バイト単位** で
処理する。日本語は UTF-8 で 1文字=3バイトのため、素の input() だと
バックスペース1回で1バイトしか消えず、壊れたマルチバイトの断片が残る。
`readline`（このリポジトリの venv では GNU readline）を import すると
input() がそのライン編集を使うようになり、UTF-8を1文字単位で削除できる。

readline は import するだけで input() のフックが差し込まれる。Windows 等で
利用できない場合は黙ってフォールバックする（コアループには影響しない）。
"""

from __future__ import annotations


def enable_line_editing() -> bool:
    """input() のマルチバイト対応ライン編集を有効化する。

    成功したら True、readline が使えなければ False を返す。
    """
    try:
        import readline  # noqa: F401  (import 副作用で input() のフックが入る)
    except ImportError:
        return False
    return True
