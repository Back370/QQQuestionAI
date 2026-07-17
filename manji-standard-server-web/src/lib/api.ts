// 手書き: API ベース。生成された client がこれを使う。
// backend(manji-standard-server-*)の REST エンドポイントに繋ぐ。
// ビルド時に NEXT_PUBLIC_API_BASE で差し替え可(Dockerfile の build arg)。
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

export async function apiFetch<T>(path: string, opts: { method: string; body?: unknown }): Promise<T> {
  const res = await fetch(BASE + path, {
    method: opts.method,
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : {},
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error("API error " + res.status);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
