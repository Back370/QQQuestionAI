from qqquestion.diff_analyzer import analyze, extract_topics


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
