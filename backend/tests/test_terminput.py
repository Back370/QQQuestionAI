import sys

from qqquestion.terminput import enable_line_editing


def test_enable_line_editing_imports_readline_for_input_editing():
    """readline が使える環境では有効化に成功し、input() のフックが入る。"""
    ok = enable_line_editing()
    if "readline" in sys.modules:
        # import できたなら必ず True を返す（副作用で input() の編集が有効になる）
        assert ok is True
    else:
        # readline を持たない環境（Windows 等）では黙って False
        assert ok is False


def test_enable_line_editing_survives_missing_readline(monkeypatch):
    """readline が無くても例外を投げずフォールバックする。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "readline":
            raise ImportError("no readline")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "readline", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert enable_line_editing() is False
