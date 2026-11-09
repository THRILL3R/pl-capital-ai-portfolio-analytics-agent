import { Info, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

export type Tone = 'indigo' | 'violet' | 'neutral' | 'positive' | 'negative' | 'dark';

const TONES: Record<Tone, string> = {
  indigo: 'bg-lavender-callout text-primary-container',
  violet: 'bg-lavender-callout text-violet-accent',
  neutral: 'bg-surface-container text-indigo-700',
  positive: 'bg-status-positive-bg text-status-positive-text',
  negative: 'bg-status-negative-bg text-status-negative-text',
  dark: 'bg-indigo-800 text-on-primary',
};

/** Small status/route badge. Green ('positive') is reserved for success and gains. */
export function Pill({ tone = 'neutral', icon, children, title }: { tone?: Tone; icon?: ReactNode; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-1 text-label-sm font-semibold ${TONES[tone]}`}>
      {icon}
      {children}
    </span>
  );
}

export function PageHeader({ crumb, eyebrow, title, children, dense = false, actions }: { crumb: string; eyebrow: string; title: string; children?: ReactNode; dense?: boolean; actions?: ReactNode }) {
  return (
    <header className={dense ? 'mb-3 flex items-end justify-between gap-3 md:mb-4' : 'mb-6'}>
      <div className="min-w-0">
      <nav aria-label="Breadcrumb" className={`mb-2 flex items-center gap-1.5 text-label-md text-text-muted ${dense ? 'max-md:hidden' : ''}`}>
        <Link to="/ask" className="hover:text-primary">Home</Link>
        <span aria-hidden="true">›</span>
        <span className="font-semibold text-indigo-800">{crumb}</span>
      </nav>
      <div className={`eyebrow mb-1 ${dense ? 'max-md:hidden' : ''}`}>{eyebrow}</div>
      <h1 className={`text-headline-kpi-mobile text-indigo-800 sm:text-headline-kpi ${dense ? 'max-sm:text-[22px] max-sm:leading-7' : ''}`}>{title}</h1>
      {children && <div className="mt-2 text-body-md text-text-secondary">{children}</div>}
      </div>
      {actions}
    </header>
  );
}

export function KpiTile({ label, value, caption, icon }: { label: string; value: ReactNode; caption?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="card flex flex-col justify-between overflow-hidden">
      <div className="p-5">
        <div className="mb-2 flex items-center justify-between text-label-sm font-semibold uppercase tracking-wider text-text-muted">
          <span>{label}</span>
          <span className="text-violet-accent">{icon}</span>
        </div>
        <div className="tabular text-[40px] font-bold leading-tight tracking-tight text-violet-accent sm:text-[48px]">{value}</div>
      </div>
      {caption && <div className="bg-lavender-callout px-5 py-2.5 text-label-sm text-indigo-700">{caption}</div>}
    </div>
  );
}

export function Callout({ title, children, tone = 'info' }: { title?: string; children: ReactNode; tone?: 'info' | 'warning' }) {
  return (
    <div className={`flex items-start gap-3 rounded-xl p-4 ${tone === 'warning' ? 'bg-status-negative-bg/40' : 'bg-lavender-callout'} border-l-4 ${tone === 'warning' ? 'border-status-negative-text' : 'border-primary-container'}`}>
      <span className="mt-0.5 shrink-0 text-primary" aria-hidden="true">
        {tone === 'warning' ? <TriangleAlert size={18} /> : <Info size={18} />}
      </span>
      <div className="text-body-sm text-text-secondary">
        {title && <div className="mb-0.5 text-label-lg font-bold text-indigo-900">{title}</div>}
        {children}
      </div>
    </div>
  );
}

export function NoData({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div role="status" className="rounded-lg bg-surface-container-low p-6 text-center">
      {icon && <div className="mb-2 flex justify-center text-text-muted">{icon}</div>}
      <p className="text-headline-sm text-indigo-900">{title}</p>
      {children && <p className="mx-auto mt-1 max-w-md text-body-sm text-text-secondary">{children}</p>}
    </div>
  );
}

export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-status-negative-text/30 bg-status-negative-bg/50 p-4">
      <div className="flex items-start gap-2.5 text-body-md text-text-primary">
        <TriangleAlert size={18} className="mt-0.5 shrink-0 text-status-negative-text" aria-hidden="true" />
        <span className="whitespace-pre-line">{message}</span>
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="rounded-full border border-indigo-700 bg-surface-base px-4 py-1.5 text-label-lg text-indigo-700 hover:bg-lavender-callout">
          Try again
        </button>
      )}
    </div>
  );
}

export function Skeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading" className="animate-pulse space-y-3">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="h-4 rounded bg-surface-container" style={{ width: `${95 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function SegmentedToggle<T extends string>({ value, options, onChange, label }: { value: T; options: { id: T; label: string }[]; onChange: (id: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-full bg-surface-container p-1">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
          className={`rounded-full px-4 py-1.5 text-label-md transition-all ${value === o.id ? 'bg-brand-blue font-semibold text-on-primary shadow-sm' : 'font-medium text-text-secondary hover:text-indigo-900'}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Disclaimer() {
  return <p className="py-3 text-center text-body-sm italic text-text-muted">Analytical result over a supplied sample dataset. Not investment advice.</p>;
}
