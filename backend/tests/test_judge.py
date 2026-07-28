from qqquestion.judge import _SYSTEM, canonical_point, judge_answer
from qqquestion.models import Judgement, Question

# issue #17 の再現に使う、要点が言い換えられやすい問題
_RETRY_QUESTION = Question(
    id="retry",
    type="implementation",
    text="なぜエラー時に500ではなく200を返しているのですか。",
    model_answer=(
        "何度リトライしても結果が変わらない種類のエラーなので、200を返して"
        "処理成功を伝え、呼び出し元のリトライを止めるため。"
    ),
    accepted_points=[
        "リトライしても結果が変わらないエラーの性質",
        "200を返して呼び出し元のリトライを止める",
    ],
    rubric="エラーの性質と、リトライを止める目的の両方に触れれば correct。",
    topic="エラーハンドリング",
    difficulty=2,
)


def test_system_prompt_forbids_verdict_in_reason():
    # reason に結論を書かせない指示が消えると correct→partial のような
    # 自己矛盾した理由がストリーム表示に漏れる（回帰ガード）
    assert "reason には結論" in _SYSTEM
    assert "判定宣言" in _SYSTEM


def test_system_prompt_hides_missing_point_content_in_reason():
    # partial の reason が欠けた要点＝答えをそのまま／言い換えで読み上げない
    # ための指示。これが消えると「〜には言及がない」と正解を列挙する漏洩が
    # 復活する（judge の reason は UI にそのまま出るため）。
    assert "欠けている要点" in _SYSTEM
    assert "言い換えて述べることも" in _SYSTEM  # 言い換えでの漏洩も禁止
    assert "抽象的に" in _SYSTEM  # 方向だけ示す


def test_exact_match_skips_llm(fake_llm, demo_questions):
    question = demo_questions[0]
    judgement = judge_answer(fake_llm, question, question.model_answer)
    assert judgement.verdict == "correct"
    assert judgement.reason == "許容解答と一致"
    assert fake_llm.calls == []  # LLM を呼ばない


def test_exact_match_normalizes_notation(fake_llm, demo_questions):
    question = demo_questions[0]
    # 全角・空白・句読点のゆれを吸収して一致させる
    noisy = "　" + question.model_answer.replace("、", " ") + "。"
    judgement = judge_answer(fake_llm, question, noisy)
    assert judgement.verdict == "correct"
    assert fake_llm.calls == []


def test_empty_answer_is_incorrect_without_llm(fake_llm, demo_questions):
    judgement = judge_answer(fake_llm, demo_questions[0], "   ")
    assert judgement.verdict == "incorrect"
    assert fake_llm.calls == []


def test_llm_judgement_used_for_free_text(fake_llm, demo_questions):
    fake_llm.enqueue(
        Judgement(verdict="partial", missing_points=["再帰結合"], reason="要点が不足")
    )
    judgement = judge_answer(fake_llm, demo_questions[0], "前の状態を使うから")
    assert judgement.verdict == "partial"
    assert fake_llm.calls[0]["temperature"] == 0.0  # 判定は temperature 0.0


def test_empty_reason_triggers_rejudge(fake_llm, demo_questions):
    fake_llm.enqueue(Judgement(verdict="correct", reason=""))
    fake_llm.enqueue(Judgement(verdict="correct", reason="要点をすべて満たす"))
    judgement = judge_answer(fake_llm, demo_questions[0], "自由記述の解答")
    assert judgement.reason == "要点をすべて満たす"
    assert len(fake_llm.calls) == 2


def test_demo_rule_judge(demo_llm, demo_questions):
    question = demo_questions[3]  # reversed の問題
    good = judge_answer(
        demo_llm, question, "deltaのt+1への依存があるので未来から過去の順で計算する"
    )
    assert good.verdict == "correct"
    bad = judge_answer(demo_llm, question, "そういう決まりだから")
    assert bad.verdict == "incorrect"


def test_already_matched_points_carry_over(fake_llm, demo_questions):
    """前回満たした要点は再度言及しなくても正解になる。"""
    question = demo_questions[0]  # 要点3つ
    # 今回の解答は残り2要点だけをカバー（「再帰結合」には触れていない）
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["前の時刻の隠れ状態", "系列・文脈の保持"],
            missing_points=["再帰結合"],
            reason="残りの要点を満たしました",
        )
    )
    judgement = judge_answer(
        fake_llm, question, "前の時刻の状態で文脈を保持する", already_matched=["再帰結合"]
    )
    assert judgement.verdict == "correct"  # 合算で全要点 → 正解
    assert set(judgement.matched_points) == set(question.accepted_points)
    assert "前回までの解答と合わせて" in judgement.reason
    # プロンプトに「既に満たした要点」が伝わっている
    assert "再度の言及を要求しないこと" in fake_llm.calls[0]["user"]


def test_partial_accumulates_but_stays_partial(fake_llm, demo_questions):
    question = demo_questions[0]
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["前の時刻の隠れ状態"],
            missing_points=["系列・文脈の保持"],
            reason="まだ足りません",
        )
    )
    judgement = judge_answer(
        fake_llm, question, "前の時刻の状態を使う", already_matched=["再帰結合"]
    )
    assert judgement.verdict == "partial"
    assert set(judgement.matched_points) == {"再帰結合", "前の時刻の隠れ状態"}
    assert judgement.missing_points == ["系列・文脈の保持"]


def test_system_prompt_requires_verbatim_points():
    # 要点を言い換えて返されると突き合わせが外れ、満たした要点が数えられない
    # （issue #17 の温床）。コピーを求める指示の回帰ガード
    assert "そのままコピー" in _SYSTEM


def test_llm_correct_is_not_demoted_to_partial(fake_llm):
    """LLM が correct と判定したものを合算で降格させない（issue #17）。

    LLM が要点を言い換えて返すと accepted_points との対応が取れず、
    「全要点に触れたのに部分的に正解」と表示されていた。
    """
    fake_llm.enqueue(
        Judgement(
            verdict="correct",
            # accepted_points のどれとも文字列対応が取れない言い換え
            matched_points=["前回の内容と合わせて必要な観点がすべて揃った"],
            missing_points=[],
            reason="必要な要素がすべて揃いました",
        )
    )
    judgement = judge_answer(
        fake_llm,
        _RETRY_QUESTION,
        "200を返して処理成功を伝えることでリトライを止めるため",
        already_matched=["リトライしても結果が変わらないエラーの性質"],
    )
    assert judgement.verdict == "correct"
    assert judgement.missing_points == []
    assert set(judgement.matched_points) == set(_RETRY_QUESTION.accepted_points)


def test_partial_with_shortened_point_is_promoted_to_correct(fake_llm):
    """要点を短く言い換えられても、合算で全要点が埋まれば正解にする（issue #17）。"""
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["リトライを止めるため"],  # 要点の短い言い換え
            missing_points=[],
            reason="目的について捉えられています",
        )
    )
    judgement = judge_answer(
        fake_llm,
        _RETRY_QUESTION,
        "200を返して処理成功を伝えることでリトライを止めるため",
        already_matched=["リトライしても結果が変わらないエラーの性質"],
    )
    assert judgement.verdict == "correct"
    assert judgement.missing_points == []


def test_canonical_point_matches_shortened_paraphrase():
    accepted = _RETRY_QUESTION.accepted_points
    assert canonical_point("リトライを止めるため", accepted) == accepted[1]
    # 似ていない文字列は取り込まない（別の要点を満たしたと誤認しない）
    assert canonical_point("ログを出力しているから", accepted) is None
    # 短すぎる断片でのファジー一致もしない
    assert canonical_point("200", accepted) is None


def test_incorrect_stays_incorrect_with_already_matched(fake_llm):
    """降格しないだけで、満たしていない解答が昇格するわけではない。"""
    fake_llm.enqueue(
        Judgement(verdict="incorrect", matched_points=[], reason="要点に届いていません")
    )
    judgement = judge_answer(
        fake_llm,
        _RETRY_QUESTION,
        "なんとなくそう書いた",
        already_matched=["リトライしても結果が変わらないエラーの性質"],
    )
    assert judgement.verdict == "partial"  # 前回分は保持したまま partial
    assert judgement.missing_points == ["200を返して呼び出し元のリトライを止める"]


def test_llm_paraphrased_points_are_canonicalized(fake_llm, demo_questions):
    """LLM が要点を言い換えて返しても accepted_points に対応付けて数える。"""
    question = demo_questions[0]
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["再帰結合について"],  # 完全一致ではない
            missing_points=[],
            reason="一部を満たす",
        )
    )
    judgement = judge_answer(fake_llm, question, "再帰結合がある")
    assert judgement.matched_points == ["再帰結合"]  # 正規の表記に揃う
