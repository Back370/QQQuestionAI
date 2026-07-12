import pytest

from qqquestion.demo import DEMO_QUESTIONS, build_demo_llm
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.llm import FakeLLM
from qqquestion.models import Chunk, DiffContext

SAMPLE_DIFF = """\
diff --git a/rnn_train.py b/rnn_train.py
index 1111111..2222222 100644
--- a/rnn_train.py
+++ b/rnn_train.py
@@ -1,4 +1,20 @@
 import numpy as np
+for epoch in range(num_epoch):
+    Z_prime = np.zeros((q, T+1))
+    for t in range(T):
+        Z_prime[:, t+1], nabla_f[:, t] = forward(np.append(1, xi[t,:]), Z_prime[:, t], W_in, W, sigmoid)
+    z_out = softmax(np.dot(W_out, Z_T))
+    e[i] = CrossEntoropy(z_out, yi)
+    delta_out = z_out - yi
+    for t in reversed(range(T)):
+        delta[:, t] = backward(W, W_out[:, 1:], delta[:, t+1], np.zeros(m), nabla_f[:, t])
+    dEdW_out = np.outer(delta_out, Z_T)
+    dEdW = np.dot(delta, Z_prime[:, :T].T)
"""


@pytest.fixture
def sample_diff() -> str:
    return SAMPLE_DIFF


@pytest.fixture
def diff_ctx(sample_diff) -> DiffContext:
    from qqquestion.diff_analyzer import analyze

    return analyze(sample_diff)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def demo_llm() -> FakeLLM:
    return build_demo_llm()


@pytest.fixture
def kb() -> InMemoryKnowledgeBase:
    store = InMemoryKnowledgeBase()
    store.add(
        "RNN",
        [
            Chunk(
                text="RNN は再帰結合を持ち、前の時刻の隠れ状態を利用して系列を処理する。",
                url="https://example.com/rnn",
                title="RNN入門",
            )
        ],
    )
    store.add(
        "誤差逆伝播",
        [
            Chunk(
                text="逆伝播では時刻を遡って delta を計算する。BPTT と呼ばれる。",
                url="https://example.com/bptt",
                title="BPTT",
            )
        ],
    )
    return store


@pytest.fixture
def demo_questions():
    return list(DEMO_QUESTIONS)
