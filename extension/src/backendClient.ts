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

export class BackendClient {
  constructor(private readonly port: number) {}

  private url(path: string): string {
    return `http://127.0.0.1:${this.port}${path}`;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(this.url(path), init);
    if (!response.ok) {
      throw new Error(`${path} -> HTTP ${response.status}: ${await response.text()}`);
    }
    return (await response.json()) as T;
  }

  async health(): Promise<boolean> {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2000);
      const response = await fetch(this.url("/health"), { signal: controller.signal });
      clearTimeout(timer);
      return response.ok;
    } catch {
      return false;
    }
  }

  pending(): Promise<{ sessions: PendingSession[] }> {
    return this.request("/quiz/pending");
  }

  question(sessionId: string): Promise<{ question: PublicQuestion | null; status: string }> {
    return this.request(`/quiz/${sessionId}/question`);
  }

  answer(sessionId: string, answer: string): Promise<AnswerResponse> {
    return this.request(`/quiz/${sessionId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
  }

  hint(sessionId: string): Promise<{ hint: { hint: string; citations: string[] } }> {
    return this.request(`/quiz/${sessionId}/hint`, { method: "POST" });
  }

  giveUp(sessionId: string): Promise<AnswerResponse> {
    return this.request(`/quiz/${sessionId}/giveup`, { method: "POST" });
  }

  abort(sessionId: string): Promise<{ status: string }> {
    return this.request(`/quiz/${sessionId}/abort`, { method: "POST" });
  }

  report(sessionId: string): Promise<{ rendered: string }> {
    return this.request(`/quiz/${sessionId}/report`);
  }
}
