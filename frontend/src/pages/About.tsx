import { Calculator, Database, Minus, Plus, ShieldCheck, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Callout, Disclaimer, PageHeader } from '../components/ui';

const FAQ = [
  {
    q: 'Why does exposure not add up to 100%?',
    a: 'The Exposure Calculator sums the stored weights of stock holdings only and never renormalises them. A portfolio that also holds bonds therefore totals less than 100% of equity exposure. The remainder is shown as “Excluded from exposure”.',
  },
  {
    q: 'What happens if Gemini is unavailable?',
    a: 'The SQL Query Tool falls back to its deterministic rule-based generator. The answer says which method was used, and a fallback is always labelled as such. The rule-based generator only covers the supplied question patterns.',
  },
  { q: 'Can this place trades?', a: 'No. It is a read-only analytics prototype. It has no broker, exchange or order connectivity.' },
  { q: 'Is this real client data?', a: 'No. It is a small sample dataset (13 portfolios, 30 US-listed securities priced in USD) supplied for the assessment.' },
];

const LIMITS = [
  'Tool routing is keyword-based, so unusual phrasings can be misrouted.',
  'The rule-based fallback covers a finite set of question patterns.',
  'Questions that need both tools at once are not supported.',
  'Everything shown is a static snapshot of the sample dataset, not live market data.',
];

function Step({ title, text, icon }: { title: string; text: string; icon?: React.ReactNode }) {
  return (
    <li className="flex flex-1 items-start gap-3 rounded-xl border border-border-hairline bg-surface-base p-4">
      <span className="mt-0.5 text-violet-accent">{icon}</span>
      <div>
        <div className="text-label-lg font-semibold text-indigo-900">{title}</div>
        <div className="text-body-sm text-text-secondary">{text}</div>
      </div>
    </li>
  );
}

export function About() {
  const [open, setOpen] = useState<number | null>(0);
  const { hash } = useLocation();
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView({ behavior: 'smooth' });
  }, [hash]);

  return (
    <>
      <PageHeader crumb="About" eyebrow="How it works" title="How the agent answers your question">
        The agent reads your question, picks one of two tools, runs it against a read-only copy of the sample database, and shows you exactly how it got the answer.
      </PageHeader>

      <section id="how-it-works" aria-label="Pipeline" className="mb-8">
        <ol className="flex flex-col gap-3 lg:flex-row lg:items-stretch">
          <Step title="1 · Your question" text="Typed in plain English." />
          <Step title="2 · Agent router" text="Keyword rules choose a tool and record why." />
          <li className="flex flex-1 flex-col gap-3">
            <Step title="SQL Query Tool" text="Gemini (or the rule-based fallback) writes one SELECT statement; it is validated, then run read-only." icon={<Database size={18} aria-hidden="true" />} />
            <Step title="Exposure Calculator" text="Deterministic equity-only sector exposure for one named portfolio." icon={<Calculator size={18} aria-hidden="true" />} />
          </li>
          <Step title="3 · Structured result" text="Rows, SQL, method, model and any fallback reason." />
        </ol>
      </section>

      <section aria-label="Principles" className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
        {[
          { icon: <ShieldCheck size={22} aria-hidden="true" />, title: 'Read-only by design', text: 'Only a single SELECT or WITH statement runs. Comments and write keywords are rejected, the database connection is read-only, query-only mode is on, and an authorizer blocks writes.' },
          { icon: <Sparkles size={22} aria-hidden="true" />, title: 'Gemini with a safe fallback', text: 'Generated SQL is grounded in the live schema and validated before it runs. If Gemini fails, the rule-based generator answers and the result says so.' },
          { icon: <Calculator size={22} aria-hidden="true" />, title: 'Deterministic exposure maths', text: 'Sector exposure is SUM(current_weight) × 100 over Stock holdings. Gemini is never used for this calculation.' },
        ].map((c) => (
          <div key={c.title} className="card p-5">
            <span className="text-violet-accent">{c.icon}</span>
            <h2 className="mt-2 text-headline-sm text-indigo-900">{c.title}</h2>
            <p className="mt-1 text-body-sm text-text-secondary">{c.text}</p>
          </div>
        ))}
      </section>

      <section aria-label="Frequently asked questions" className="mb-8">
        <div className="eyebrow mb-3">Questions</div>
        <div className="card divide-y divide-border-hairline">
          {FAQ.map((f, i) => (
            <div key={f.q}>
              <h3>
                <button type="button" aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)} className="flex w-full items-center justify-between gap-4 px-5 py-4 text-left text-headline-sm text-indigo-900">
                  {f.q}
                  {open === i ? <Minus size={18} aria-hidden="true" /> : <Plus size={18} aria-hidden="true" />}
                </button>
              </h3>
              {open === i && <p className="px-5 pb-4 text-body-md text-text-secondary">{f.a}</p>}
            </div>
          ))}
        </div>
      </section>

      <section aria-label="Limitations" className="mb-4">
        <Callout title="Known limitations">
          <ul className="mt-1 list-disc space-y-1 pl-5">{LIMITS.map((l) => <li key={l}>{l}</li>)}</ul>
        </Callout>
      </section>
      <Disclaimer />
    </>
  );
}
