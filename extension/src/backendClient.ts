// バックエンド (FastAPI ローカルサーバ) への HTTP クライアント。
// architecture.md §3: 拡張 ⇔ バックエンドは localhost HTTP (JSON)。

export interface PendingSession {
  session_id: string;
  topics: string[];
  files: string[];
  total: number;
}

export interface PublicQuestion {
  id: string;
  type: "prerequisite" | "implementation";
  text: string;
  code_snippet: string | null;
  topic: string;
  difficulty: number;
  number: number;
  total: number;
  hint_level: number;
}

export interface Judgement {
  verdict: "correct" | "partial" | "incorrect";
  matched_points: string[];
  missing_points: string[];
  reason: string;
}

export interface AnswerResponse {
  judgement: Judgement;
  question_done: boolean;
  model_answer?: string;
  explanation?: { explanation: string; citations: string[] } | null;
  next_question: PublicQuestion | null;
  status: string;
}

// /answer/stream, /giveup/stream (SSE) の1イベント。
// event: "judgement_partial" | "judgement" | "explanation_partial" | "result" | "error"
// "error" は AI が使えなくなったときの終端イベント（message に利用者向けの理由）。
export interface StreamEvent {
  event: string;
  reason?: string;
  explanation?: string | AnswerResponse["explanation"];
  judgement?: Judgement;
  question_done?: boolean;
  model_answer?: string;
  next_question?: PublicQuestion | null;
  status?: string;
  message?: string;
}

// バックエンドの AI が使えない (HTTP 503)。キーの失効・レート制限・モデル提供終了
// など、待っても直らない失敗なので、呼び出し側は再試行せず警告を出して畳む。
// message はバックエンドが分類した日本語で、そのまま利用者に見せられる。
export class LlmUnavailableError extends Error {}

// 通常エンドポイント（DB/メモリ参照のみ）の応答待ちタイムアウト
const DEFAULT_TIMEOUT_MS = 10_000;
// LLM 呼び出しを伴うエンドポイントは生成に時間がかかるため長めに待つ
const LLM_TIMEOUT_MS = 60_000;
// ポーリングは 1.5 秒ごとに繰り返すので、ハングしたバックエンドを 10 秒待つと
// 呼び出しが積み上がる。次の周回で取り返せるので短く諦める
const POLL_TIMEOUT_MS = 2_000;

// 失敗レスポンスを例外に変換する。503（AI が使えない）だけは、バックエンドが
// 作った日本語の detail をそのまま警告に使えるよう専用の型で返す。
async function failureOf(path: string, response: Response): Promise<Error> {
  const body = await response.text();
  if (response.status === 503) {
    return new LlmUnavailableError(detailOf(body) ?? body);
  }
  return new Error(`${path} -> HTTP ${response.status}: ${body}`);
}

function detailOf(body: string): string | undefined {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : undefined;
  } catch {
    return undefined;
  }
}

export class BackendClient {
  constructor(private readonly port: number) {}

  private url(path: string): string {
    return `http://127.0.0.1:${this.port}${path}`;
  }

  private async fetchWithTimeout(
    path: string,
    init: RequestInit | undefined,
    timeoutMs: number
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(this.url(path), { ...init, signal: controller.signal });
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(
          `${path} -> ${Math.round(timeoutMs / 1000)}秒以内に応答がありませんでした。` +
            "バックエンドがハングしているか、負荷が高い可能性があります。"
        );
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  private async request<T>(
    path: string,
    init?: RequestInit,
    timeoutMs: number = DEFAULT_TIMEOUT_MS
  ): Promise<T> {
    const response = await this.fetchWithTimeout(path, init, timeoutMs);
    if (!response.ok) {
      throw await failureOf(path, response);
    }
    return (await response.json()) as T;
  }

  async health(): Promise<boolean> {
    try {
      const response = await this.fetchWithTimeout("/health", undefined, 2000);
      return response.ok;
    } catch {
      return false;
    }
  }

  // クイズを開始する（コミットとは無関係。結果がコミットを左右することはない）。
  // 第1問はサーバ側で同期生成されるため、LLM 相当の待ち時間を見込む。
  start(repoPath: string): Promise<{
    session_id: string;
    topics: string[];
    files: string[];
    total: number;
    error?: string | null;
  }> {
    return this.request(
      "/quiz/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // origin="ui": 呼び出し元（startQuiz）が自分でパネルを開くので、
        // /quiz/pending のポーリングには載せない
        body: JSON.stringify({ repo_path: repoPath, origin: "ui" }),
      },
      LLM_TIMEOUT_MS
    );
  }

  // このウィンドウのワークスペースパスを渡し、コミットが走ったリポジトリと
  // 一致するセッションだけ受け取る（別ウィンドウでパネルが開くのを防ぐ）。
  // バックエンド未起動なら例外になるので、呼び出し側の health 確認は要らない。
  pending(workspaces: string[] = []): Promise<{ sessions: PendingSession[] }> {
    const query = workspaces
      .map((w) => `workspace=${encodeURIComponent(w)}`)
      .join("&");
    return this.request(
      `/quiz/pending${query ? `?${query}` : ""}`,
      undefined,
      POLL_TIMEOUT_MS
    );
  }

  // パネルが表示準備を終えた後に所有権を取る。GET /quiz/pending は読み取り専用で
  // 何度でも同じセッションを返すため、実際に開けたパネルがこの POST で claim して
  // 初めて一覧から外れる。ok=false は別ウィンドウが先に所有した合図。
  claim(sessionId: string): Promise<{ ok: boolean }> {
    return this.request(
      `/quiz/${sessionId}/claim`,
      { method: "POST" },
      POLL_TIMEOUT_MS
    );
  }

  question(sessionId: string): Promise<{
    question: PublicQuestion | null;
    status: string;
    preparing?: boolean;
    error?: string | null;
  }> {
    return this.request(`/quiz/${sessionId}/question`);
  }

  answer(sessionId: string, answer: string): Promise<AnswerResponse> {
    return this.request(
      `/quiz/${sessionId}/answer`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      },
      LLM_TIMEOUT_MS
    );
  }

  // 半二重ストリーミング版。SSE イベントを受信した側から onEvent に渡す
  answerStream(
    sessionId: string,
    answer: string,
    onEvent: (event: StreamEvent) => void
  ): Promise<void> {
    return this.stream(
      `/quiz/${sessionId}/answer/stream`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      },
      onEvent
    );
  }

  giveUpStream(sessionId: string, onEvent: (event: StreamEvent) => void): Promise<void> {
    return this.stream(`/quiz/${sessionId}/giveup/stream`, { method: "POST" }, onEvent);
  }

  private async stream(
    path: string,
    init: RequestInit,
    onEvent: (event: StreamEvent) => void
  ): Promise<void> {
    // 接続確立（レスポンスヘッダ受信）までにタイムアウトを適用する。
    // ボディは LLM の逐次生成で長時間続きうるため、受信開始後は打ち切らない
    const response = await this.fetchWithTimeout(path, init, DEFAULT_TIMEOUT_MS);
    if (!response.ok) {
      throw await failureOf(path, response);
    }
    if (!response.body) {
      throw new Error(`${path} -> 空のレスポンスボディ`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminated = false;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, boundary).trim();
        buffer = buffer.slice(boundary + 2);
        if (raw.startsWith("data: ")) {
          const event = JSON.parse(raw.slice("data: ".length)) as StreamEvent;
          terminated = terminated || event.event === "result" || event.event === "error";
          onEvent(event);
        }
      }
    }
    // ヘッダ送出後にバックエンドが落ちると、接続が切れるだけで終端イベントが
    // 来ない。呼び出し側は待つのをやめられず固まるので、ここで失敗にする
    if (!terminated) {
      throw new Error(
        `${path} -> 応答が途中で切れました。バックエンドの出力（QQQuestionAI）を確認してください。`
      );
    }
  }

  hint(sessionId: string): Promise<{ hint: { hint: string; citations: string[] } }> {
    return this.request(`/quiz/${sessionId}/hint`, { method: "POST" }, LLM_TIMEOUT_MS);
  }

  giveUp(sessionId: string): Promise<AnswerResponse> {
    return this.request(`/quiz/${sessionId}/giveup`, { method: "POST" }, LLM_TIMEOUT_MS);
  }

  abort(sessionId: string): Promise<{ status: string }> {
    return this.request(`/quiz/${sessionId}/abort`, { method: "POST" });
  }

  report(sessionId: string): Promise<{
    rendered: string;
    status: string;
    attempted: number;
    completed: boolean;
  }> {
    return this.request<{
      rendered: string;
      status: string;
      report: { attempted: number; completed: boolean };
    }>(`/quiz/${sessionId}/report`, undefined, LLM_TIMEOUT_MS).then((body) => ({
      rendered: body.rendered,
      status: body.status,
      attempted: body.report.attempted,
      completed: body.report.completed,
    }));
  }
}
