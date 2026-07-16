import subprocess

from qqquestion import diff_analyzer
from qqquestion.diff_analyzer import analyze, analyze_staged, extract_topics, get_staged_diff


def test_extract_topics_from_rnn_diff(sample_diff):
    topics = extract_topics(sample_diff)
    assert "RNN" in topics
    assert "softmax" in topics
    assert "クロスエントロピー" in topics  # 教材コードの綴りゆれ CrossEntoropy を拾う
    assert "誤差逆伝播" in topics


def test_topics_are_unique_and_capped(sample_diff):
    topics = extract_topics(sample_diff)
    assert len(topics) == len(set(topics))
    assert len(topics) <= 6


def test_analyze_extracts_files(sample_diff):
    ctx = analyze(sample_diff)
    assert ctx.files == ["rnn_train.py"]
    assert ctx.diff_text == sample_diff


def test_removed_lines_do_not_count():
    diff = """\
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-softmax_layer()
+plain_function()
"""
    assert "softmax" not in extract_topics(diff)


def test_fallback_to_function_names():
    diff = """\
--- a/x.py
+++ b/x.py
@@ -0,0 +1,2 @@
+def calculate_total_price(items):
+    return sum(items)
"""
    topics = extract_topics(diff)
    assert topics == ["calculate total price"]


def _staged_repo(tmp_path, filename: str, data: bytes):
    """ファイルを1つステージ済みにした git リポジトリを作る。"""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    (tmp_path / filename).write_bytes(data)
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    return str(tmp_path)


def test_japanese_diff_is_decoded_as_utf8(tmp_path):
    """日本語コメントを含む差分でも壊れない（locale が cp932 でも UTF-8 で読む）。"""
    src = "public class OurBoard {\n    // フロンティア判定: Zobrist 乱数表\n}\n"
    repo = _staged_repo(tmp_path, "OurBoard.java", src.encode("utf-8"))

    ctx = analyze_staged(repo)

    assert ctx.files == ["OurBoard.java"]
    assert "フロンティア判定" in ctx.diff_text


def test_non_utf8_file_does_not_crash(tmp_path):
    """Shift_JIS で保存されたファイルの差分でも例外にせず読み飛ばす。"""
    repo = _staged_repo(tmp_path, "legacy.java", "// 盤面の評価\n".encode("cp932"))

    diff = get_staged_diff(repo)

    assert isinstance(diff, str)
    assert "legacy.java" in diff


def test_git_output_is_read_with_explicit_utf8(tmp_path, monkeypatch):
    """encoding 未指定に戻すと Windows で stdout が None になり
    analyze() が TypeError で落ちるため、明示を回帰テストで固定する。"""
    captured: dict = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(diff_analyzer.subprocess, "run", spy)
    get_staged_diff(_staged_repo(tmp_path, "a.py", b"x = 1\n"))

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
