import type { ReactNode } from "react";

interface AsyncStateProps {
  kind: "loading" | "error" | "empty";
  title: string;
  detail?: string;
  children?: ReactNode;
}

export function AsyncState({ kind, title, detail, children }: AsyncStateProps) {
  return (
    <section className={`async-state async-state--${kind}`} aria-live="polite">
      <span className="async-state__signal" aria-hidden="true" />
      <div>
        <h2>{title}</h2>
        {detail ? <p>{detail}</p> : null}
        {children}
      </div>
    </section>
  );
}
