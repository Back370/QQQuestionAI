// 手書き: componentMap(widget 実体)。生成UIと業務固有画面の両方がこれに委譲する。
// 見た目/テーマはここ + tokens.css で持つ = 生成/手書きの境界(構造は生成、widget は手書き)。
// 原則(KB: selection-design-pattern): 必要最小限 / 優先度駆動(primaryを突出) / 列挙にしない。
// 値は tokens.css の CSS 変数のみ参照する(生の hex/px を書かない)。
import type { CSSProperties, ReactElement, ReactNode } from "react";
import Link from "next/link";

/* ---------- layout ---------- */

// シェル(サイドバー)内で使う想定 = 中央寄せせず左揃えで幅を使う。
// title は大きく、その下に description(概要)を置ける。
export function Page({ title, description, crumb, actions, children }: {
  title: string; description?: ReactNode; crumb?: ReactNode; actions?: ReactNode; children: ReactNode;
}): ReactElement {
  return (
    <div style={{ maxWidth: 1120, padding: "var(--sp-6)" }}>
      {crumb && <div style={{ fontSize: "var(--text-xs)", color: "var(--text-faint)", marginBottom: "var(--sp-2)" }}>{crumb}</div>}
      <header style={{ marginBottom: "var(--sp-5)" }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: "var(--sp-3)" }}>
          <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", letterSpacing: "-0.02em", lineHeight: 1.15 }}>{title}</h1>
          {actions && <div style={{ display: "flex", gap: "var(--sp-2)", flexShrink: 0 }}>{actions}</div>}
        </div>
        {description && (
          <p style={{ color: "var(--text-muted)", fontSize: "var(--text-md)", marginTop: "var(--sp-2)", marginBottom: 0, maxWidth: 680 }}>
            {description}
          </p>
        )}
      </header>
      {children}
    </div>
  );
}

export function Section({ title, action, children }: {
  title: string; action?: ReactNode; children: ReactNode;
}): ReactElement {
  return (
    <section style={{ marginTop: "var(--sp-5)" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: "var(--sp-2)", marginBottom: "var(--sp-2)" }}>
        <h2 style={{ margin: 0, fontSize: "var(--text-lg)" }}>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/* ---------- list(優先度カード) ---------- */

export function ListRow({ href, primary, secondary, leading, dimmed }: {
  href?: string; primary: ReactNode; secondary?: ReactNode; leading?: ReactNode; dimmed?: boolean;
}): ReactElement {
  const body = (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "var(--radius-md)",
      padding: "var(--sp-3) var(--sp-4)", boxShadow: "var(--shadow-sm)",
      display: "flex", alignItems: "center", gap: "var(--sp-3)", opacity: dimmed ? 0.6 : 1,
    }}>
      {leading}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 650, fontSize: "var(--text-md)" }}>{primary}</div>
        {secondary && <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", marginTop: 2 }}>{secondary}</div>}
      </div>
    </div>
  );
  return href
    ? <Link href={href} style={{ textDecoration: "none", color: "inherit" }}>{body}</Link>
    : body;
}

export function ListStack({ children }: { children: ReactNode }): ReactElement {
  return <div style={{ display: "grid", gap: "var(--sp-2)" }}>{children}</div>;
}

export function EmptyState({ message, action }: { message: string; action?: ReactNode }): ReactElement {
  return (
    <div style={{
      border: "1px dashed var(--border-strong)", borderRadius: "var(--radius-md)",
      padding: "var(--sp-5)", textAlign: "center", color: "var(--text-faint)", fontSize: "var(--text-sm)",
    }}>
      <div>{message}</div>
      {action && <div style={{ marginTop: "var(--sp-2)" }}>{action}</div>}
    </div>
  );
}

/* ---------- detail ---------- */

export function KVList({ items }: { items: { k: string; v: ReactNode }[] }): ReactElement {
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "var(--sp-1) var(--sp-4)", margin: 0 }}>
      {items.map(({ k, v }) => (
        <KVRow key={k} k={k} v={v} />
      ))}
    </dl>
  );
}

function KVRow({ k, v }: { k: string; v: ReactNode }): ReactElement {
  return (
    <>
      <dt style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>{k}</dt>
      <dd style={{ margin: 0, fontSize: "var(--text-sm)" }}>{v}</dd>
    </>
  );
}

/* ---------- indicators ---------- */

export type Tone = "accent" | "ok" | "warn" | "danger" | "neutral";
const toneBg: Record<Tone, string> = {
  accent: "var(--accent-soft)", ok: "var(--ok-soft)", warn: "var(--warn-soft)",
  danger: "var(--danger-soft)", neutral: "var(--surface-2)",
};
const toneFg: Record<Tone, string> = {
  accent: "var(--accent)", ok: "var(--ok)", warn: "var(--warn)",
  danger: "var(--danger)", neutral: "var(--text-muted)",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }): ReactElement {
  return (
    <span style={{
      fontSize: "var(--text-xs)", background: toneBg[tone], color: toneFg[tone],
      borderRadius: 999, padding: "2px var(--sp-2)", whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

export function ProgressBar({ value, tone = "accent" }: { value: number; tone?: Tone }): ReactElement {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div style={{ flex: 1, height: 8, background: "var(--surface-2)", borderRadius: 999, overflow: "hidden" }}>
      <div style={{ width: clamped + "%", height: "100%", background: toneFg[tone], transition: "width .2s" }} />
    </div>
  );
}

/* ---------- form ---------- */

export function Field({ label, type, value, onChange }: {
  label: string; type: string; value: string; onChange: (v: string) => void;
}): ReactElement {
  const readOnly = type === "readonly";
  const inputType = type === "email" ? "email" : "text";
  const inputStyle: CSSProperties = {
    background: "var(--surface)", border: "1px solid var(--border-strong)",
    borderRadius: "var(--radius-sm)", padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-md)",
    outlineColor: "var(--accent)",
  };
  return (
    <label style={{ display: "grid", gap: "var(--sp-1)" }}>
      <span style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>{label}</span>
      {type === "textarea" ? (
        <textarea value={value} onChange={(e) => onChange(e.target.value)} readOnly={readOnly} rows={3} style={inputStyle} />
      ) : (
        <input type={inputType} value={value} onChange={(e) => onChange(e.target.value)} readOnly={readOnly} style={inputStyle} />
      )}
    </label>
  );
}

export function Button({ variant = "primary", type = "button", onClick, children }: {
  variant?: "primary" | "ghost" | "danger"; type?: "button" | "submit";
  onClick?: () => void; children: ReactNode;
}): ReactElement {
  const styles: Record<string, CSSProperties> = {
    primary: { background: "var(--accent)", color: "var(--on-accent)", border: "1px solid transparent" },
    ghost: { background: "transparent", color: "var(--text-muted)", border: "1px solid var(--border-strong)" },
    danger: { background: "transparent", color: "var(--danger)", border: "1px solid var(--danger)" },
  };
  return (
    <button type={type} onClick={onClick} style={{
      ...styles[variant], borderRadius: "var(--radius-sm)", padding: "var(--sp-2) var(--sp-4)",
      fontSize: "var(--text-sm)", fontWeight: 600, cursor: "pointer",
    }}>{children}</button>
  );
}

export function ErrorText({ children }: { children: ReactNode }): ReactElement {
  return <div style={{ color: "var(--danger)", fontSize: "var(--text-sm)" }}>{children}</div>;
}
