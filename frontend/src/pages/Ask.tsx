import { ArrowRight, ArrowUpRight, Brain, Calculator, Check, Copy, Database, Download, FlaskConical, Layers, ListChecks, MessageCircle, PanelRight, Search, ShieldCheck, X } from 'lucide-react';
import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, api } from '../api/client';
import type { AgentResponse, ConversationResult, ExposureResult, HybridResult, ScenarioResult, SqlResult } from '../api/types';
import { DataTable } from '../components/DataTable';
import { ScenarioPanel } from '../components/ScenarioPanel';
import { SqlCode } from '../components/SqlCode';
import { AnswerVerificationStatus, ExposureCharts, ExposureKpis, VerificationStatus } from '../components/ExposurePanels';
import { Callout, ErrorNotice, NoData, PageHeader, Pill, Skeleton } from '../components/ui';
import { toCsv } from '../lib/csv';
import { formatCell, hasCurrencyColumn, humanizeColumn } from '../lib/format';

export const SUGGESTIONS = [
  'How many portfolios do we have in total?',
  'What are the names of all active portfolios?',
  'Show me the top 5 holdings by cost basis in the Growth Equity Fund',
  'What are the sector exposures for the Tech Innovation Fund?',
  'Calculate the sector exposure breakdown for international equity',
];

interface Exchange {
  id: number;
  question: string;
  status: 'loading' | 'done' | 'failed';
  response?: AgentResponse;
  networkError?: string;
}

const isSql = (r: AgentResponse): r is AgentResponse & { result: SqlResult } => r.selected_tool === 'sql_query' && r.result !== null;
const isConversation = (r: AgentResponse): r is AgentResponse & { result: ConversationResult } => r.status === 'conversation' && r.result !== null && r.result.tool === 'conversation';
const isHybrid = (r: AgentResponse): r is AgentResponse & { result: HybridResult } => r.selected_tool === 'hybrid_analysis' && r.result !== null && r.result.tool === 'hybrid_analysis';
const isScenario = (r: AgentResponse): r is AgentResponse & { result: ScenarioResult } => r.selected_tool === 'scenario_analysis' && r.result !== null && r.result.tool === 'scenario_analysis';
const isExposure = (r: AgentResponse): r is AgentResponse & { result: ExposureResult } => r.selected_tool === 'exposure_calculator' && r.result !== null;

function CopyButton({ label, text, icon }: { label: string; text: () => string; icon: 'copy' | 'download' }) {
  const [done, setDone] = useState(false);
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text());
      setDone(true);
      window.setTimeout(() => setDone(false), 1800);
    } catch {
      /* clipboard unavailable: nothing to do */
    }
  };
  return (
    <button type="button" onClick={onClick} className="inline-flex items-center gap-1.5 rounded-full bg-surface-container-low px-3 py-1 text-label-md text-indigo-800 transition-all hover:bg-surface-container">
      {done ? <Check size={14} aria-hidden="true" /> : icon === 'copy' ? <Copy size={14} aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}
      <span>{done ? 'Copied' : label}</span>
    </button>
  );
}

function StatusPill({ status }: { status: AgentResponse['status'] }) {
  if (status === 'success') return <Pill tone="positive" icon={<Check size={13} aria-hidden="true" />}>SUCCESS</Pill>;
  if (status === 'error') return <Pill tone="negative">ERROR</Pill>;
  if (status === 'conversation') return <Pill tone="neutral">CHAT</Pill>;
  return <Pill tone="neutral">{status.replace('_', ' ').toUpperCase()}</Pill>;
}

function RouteBadges({ r }: { r: AgentResponse }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {r.selected_tool === 'sql_query' && <Pill tone="indigo" icon={<Database size={14} aria-hidden="true" />}>SQL Query Tool</Pill>}
      {r.selected_tool === 'exposure_calculator' && <Pill tone="violet" icon={<Calculator size={14} aria-hidden="true" />}>Exposure Calculator · Deterministic</Pill>}
      {isSql(r) && r.result.generation_method === 'gemini' && <Pill tone="dark" icon={<Brain size={14} aria-hidden="true" />}>Gemini</Pill>}
      {isSql(r) && r.result.generation_method === 'rule_based' && <Pill tone="neutral" icon={<ListChecks size={14} aria-hidden="true" />}>Rule-based</Pill>}
      {isSql(r) && r.result.fallback_used && <Pill tone="neutral" title={r.result.fallback_reason ?? undefined}>Fallback used</Pill>}
      {r.selected_tool === 'scenario_analysis' && <Pill tone="violet" icon={<FlaskConical size={14} aria-hidden="true" />}>Scenario · hypothetical</Pill>}
      {r.selected_tool === 'hybrid_analysis' && <Pill tone="violet" icon={<Layers size={14} aria-hidden="true" />}>Hybrid · SQL + Exposure</Pill>}
      {isHybrid(r) && r.result.components.sql?.generation_method === 'gemini' && <Pill tone="dark" icon={<Brain size={14} aria-hidden="true" />}>Gemini (SQL part)</Pill>}
      {isHybrid(r) && r.result.components.sql?.fallback_used && <Pill tone="neutral" title={r.result.components.sql.fallback_reason ?? undefined}>Fallback used</Pill>}
      {isSql(r) && <AnswerVerificationStatus verification={r.result.answer_verification} />}
      {isExposure(r) && (r.result.answer_verification?.status === 'failed' && r.result.verification?.status === 'verified'
        ? <AnswerVerificationStatus verification={r.result.answer_verification} />
        : <><VerificationStatus verification={r.result.verification} />{!r.result.verification && <AnswerVerificationStatus verification={r.result.answer_verification} />}</>)}
      {isHybrid(r) && <AnswerVerificationStatus verification={r.result.answer_verification} partial={r.status === 'partial'} />}
      {isScenario(r) && r.result.answer_verification?.status === 'verified' && <AnswerVerificationStatus verification={r.result.answer_verification} />}
      <StatusPill status={r.status} />
    </div>
  );
}

function SqlAnswer({ result }: { result: SqlResult }) {
  const sql = result.generated_sql ?? result.sql ?? '';
  const scalar = result.rows.length === 1 && result.columns.length === 1 && typeof result.rows[0][result.columns[0]] === 'number';
  return (
    <>
      {result.rows.length === 0 ? (
        <NoData title="No rows returned">The query ran successfully and matched no records.</NoData>
      ) : scalar ? (
        <div className="rounded-lg bg-surface-container-low p-5">
          <div className="tabular text-[40px] font-bold leading-tight text-violet-accent sm:text-[48px]">{formatCell(result.columns[0], result.rows[0][result.columns[0]])}</div>
          <div className="text-label-md text-text-muted">{humanizeColumn(result.columns[0])}</div>
        </div>
      ) : (
        <>
          <DataTable columns={result.columns} rows={result.rows} caption="Query result" />
          <div className="mt-3 flex items-center justify-between px-1 text-body-sm text-text-muted">
            <span>Showing {result.row_count} {result.row_count === 1 ? 'row' : 'rows'}</span>
            {hasCurrencyColumn(result.columns, result.rows) && <span>Currency: USD</span>}
          </div>
        </>
      )}
      {sql && (
        <details open className="mt-5 rounded-lg bg-surface-container-low p-4">
          <summary className="flex cursor-pointer items-center justify-between text-table-header uppercase tracking-wider text-text-muted">
            <span className="flex items-center gap-1.5"><Database size={16} className="text-indigo-700" aria-hidden="true" /> Generated SQL</span>
          </summary>
          <SqlCode sql={sql} params={result.query_parameters} />
          {result.query_parameters.length > 0 && (
            <div className="mt-2.5 flex flex-wrap items-center gap-2">
              <span className="text-label-sm text-text-muted">Bound parameters:</span>
              {result.query_parameters.map((p, i) => (
                <span key={i} className="rounded bg-lavender-input px-2 py-0.5 font-mono text-label-sm font-semibold text-indigo-800">{`param_${i + 1}: ${JSON.stringify(p)}`}</span>
              ))}
            </div>
          )}
          <div className="mt-3"><CopyButton label="Copy SQL" text={() => sql} icon="copy" /></div>
        </details>
      )}
    </>
  );
}

function HybridAnswer({ result }: { result: HybridResult }) {
  const { sql, exposure } = result.components;
  const exposureOk = exposure !== undefined && (exposure.status === 'success' || exposure.status === 'no_equity_holdings');
  return (
    <div className="space-y-6">
      <p className="text-headline-sm text-text-primary">{result.portfolio ? `${result.portfolio.portfolio_name}: holdings and sector exposure` : 'Holdings and sector exposure'}</p>
      {!result.complete && (
        <Callout tone="warning" title={result.status === 'partial' ? 'Partial result: not everything you asked for could be completed' : 'Neither part could be completed'}>
          {result.summary}
        </Callout>
      )}
      <section aria-label="Holdings (SQL component)">
        <h3 className="mb-3 text-label-lg font-bold text-indigo-900">Holdings (database query)</h3>
        {sql && sql.status === 'success' ? <SqlAnswer result={sql} /> : <ErrorNotice message={`The database part could not be completed${sql?.error ? `: ${sql.error}` : '.'}`} />}
      </section>
      <section aria-label="Sector exposure (deterministic component)" className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-label-lg font-bold text-indigo-900">Sector exposure (deterministic)</h3>
          <VerificationStatus verification={exposure?.verification} />
        </div>
        {exposure && exposureOk && exposure.status === 'success' && (
          <>
            <ExposureKpis result={exposure} />
            <ExposureCharts result={exposure} />
          </>
        )}
        {exposure && exposure.status === 'no_equity_holdings' && <NoData title="No equity holdings">{exposure.error ?? 'This portfolio has no stock holdings mapped to a sector, so there is no equity exposure to show.'}</NoData>}
        {!exposureOk && <ErrorNotice message={exposure?.error ?? 'The exposure part could not be completed.'} />}
      </section>
    </div>
  );
}

function AnswerCard({ ex, selected, canSelect, onSelect, onRetry, onSuggest }: { ex: Exchange; selected: boolean; canSelect: boolean; onSelect: () => void; onRetry: () => void; onSuggest: (q: string) => void }) {
  const r = ex.response;
  return (
    <div className={`card p-5 sm:p-6 ${selected ? 'ring-2 ring-violet-accent/40' : ''}`} aria-live="polite">
      {ex.status === 'loading' && <Skeleton lines={5} />}
      {ex.status === 'failed' && <ErrorNotice message={ex.networkError ?? 'The request failed.'} onRetry={onRetry} />}
      {ex.status === 'done' && r && (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <RouteBadges r={r} />
            <div className="flex items-center gap-2">
              {canSelect && !selected && <button type="button" onClick={onSelect} className="rounded-full px-3 py-1 text-label-md text-violet-accent hover:bg-lavender-callout">Inspect</button>}
              {isSql(r) && r.result.rows.length > 0 && <CopyButton label="Copy as CSV" text={() => toCsv(r.result.columns, r.result.rows)} icon="download" />}
            </div>
          </div>

          {r.status === 'success' && isSql(r) && (
            <>
              <p className="text-headline-sm text-text-primary">{r.result.answer_verification?.summary || 'Result for your question'}</p>
              {r.result.query_description && (
                <p className="mb-4 mt-1 text-body-sm text-text-muted"><span className="font-semibold">Model-generated description (not verified):</span> {r.result.query_description}</p>
              )}
              {!r.result.query_description && <div className="mb-4" />}
              <SqlAnswer result={r.result} />
            </>
          )}
          {isExposure(r) && r.status === 'success' && (
            <div className="space-y-5">
              <p className="text-headline-sm text-text-primary">{`${r.result.portfolio_name}: ${r.result.exposures.length} ${r.result.exposures.length === 1 ? 'sector' : 'sectors'}, total equity weight ${r.result.total_equity_weight_pct}%`}</p>
              <ExposureKpis result={r.result} />
              <ExposureCharts result={r.result} />
              <Link to={`/exposure?portfolio=${encodeURIComponent(r.result.portfolio_name)}`} className="inline-flex items-center gap-1 text-label-md text-violet-accent hover:text-brand-blue">
                Open in the Exposure workspace <ArrowUpRight size={14} aria-hidden="true" />
              </Link>
            </div>
          )}
          {isHybrid(r) && <HybridAnswer result={r.result} />}
          {isScenario(r) && <ScenarioPanel result={r.result} />}
          {isConversation(r) && (
            <div className="space-y-3">
              <p className={`flex items-start gap-2.5 text-text-primary ${r.result.message.length > 110 ? 'text-body-lg' : 'text-headline-sm'}`}>
                <MessageCircle size={22} className="mt-0.5 shrink-0 text-violet-accent" aria-hidden="true" />
                <span>{r.result.message}</span>
              </p>
              {r.result.details && r.result.details.length > 0 && (
                <ul className="space-y-2 rounded-lg bg-surface-container-low p-4 text-body-md text-text-secondary" aria-label="What I can do">
                  {r.result.details.map((d) => (
                    <li key={d} className="flex items-start gap-2.5"><span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-700" aria-hidden="true" /><span>{d}</span></li>
                  ))}
                </ul>
              )}
              {r.result.suggested_questions.length > 0 && (
                <div className="flex flex-wrap gap-2" role="group" aria-label="Example questions">
                  {r.result.suggested_questions.map((s) => (
                    <button key={s} type="button" onClick={() => onSuggest(s)} className="rounded-full border border-lavender-light px-3.5 py-1.5 text-left text-label-md text-indigo-800 hover:bg-lavender-callout">{s}</button>
                  ))}
                </div>
              )}
            </div>
          )}
          {r.status === 'no_equity_holdings' && !isScenario(r) && <NoData title="No equity holdings">{r.error ?? 'This portfolio has no stock holdings mapped to a sector, so there is no equity exposure to show.'}</NoData>}
          {r.status === 'unsupported' && (
            <div className="space-y-3">
              <Callout title="This question is not supported">{r.error ?? 'I can answer questions about portfolios, holdings, securities, sectors and sector exposure.'}</Callout>
              <div className="flex flex-wrap gap-2">
                {SUGGESTIONS.slice(0, 3).map((s) => (
                  <button key={s} type="button" onClick={() => onSuggest(s)} className="rounded-full border border-lavender-light px-3.5 py-1.5 text-label-md text-indigo-800 hover:bg-lavender-callout">{s}</button>
                ))}
              </div>
            </div>
          )}
          {r.status === 'error' && !isHybrid(r) && !isScenario(r) && <ErrorNotice message={r.error ?? 'The agent could not answer this question.'} onRetry={onRetry} />}
        </>
      )}
    </div>
  );
}

type StepState = 'done' | 'pending' | 'failed';

function pipelineFor(r: AgentResponse | undefined): { name: string; sub: string; state: StepState }[] {
  if (!r) return [];
  const ok = r.status === 'success' || r.status === 'no_equity_holdings';
  const routed = r.selected_tool !== null;
  // Per-stage timing/outcome is not reported by the backend, so middle steps are only
  // marked complete when the whole request succeeded.
  const tail = (): StepState => (ok ? 'done' : 'pending');
  if (r.selected_tool === 'exposure_calculator') {
    return [
      { name: 'Question', sub: 'Received', state: 'done' },
      { name: 'Route', sub: 'Exposure Calculator', state: 'done' },
      { name: 'Calculate', sub: 'Deterministic, equity only', state: tail() },
      { name: 'Result', sub: ok ? 'Exposure returned' : 'Not produced', state: ok ? 'done' : 'failed' },
    ];
  }
  if (r.selected_tool === 'scenario_analysis') {
    const ok = r.status === 'success' || r.status === 'no_equity_holdings';
    return [
      { name: 'Question', sub: 'Received', state: 'done' },
      { name: 'Route', sub: 'Scenario tool (no Gemini)', state: 'done' },
      { name: 'Calculate', sub: 'Weight x shock, not renormalised', state: ok ? 'done' : 'failed' },
      { name: 'Verify', sub: ok ? 'Independently recomputed' : 'Withheld or failed', state: ok ? 'done' : 'failed' },
      { name: 'Result', sub: ok ? 'Hypothetical impact returned' : 'Not produced', state: ok ? 'done' : 'failed' },
    ];
  }
  if (r.selected_tool === 'hybrid_analysis') {
    const h = isHybrid(r) ? r.result : null;
    const sqlOk = h?.component_status.sql === 'success';
    const expOk = h?.component_status.exposure === 'success' || h?.component_status.exposure === 'no_equity_holdings';
    return [
      { name: 'Question', sub: 'Received', state: 'done' },
      { name: 'Route', sub: 'Hybrid: SQL + Exposure', state: 'done' },
      { name: 'Exposure', sub: expOk ? 'Calculated and verified' : 'Withheld or failed', state: expOk ? 'done' : 'failed' },
      { name: 'SQL', sub: sqlOk ? `${h?.components.sql?.row_count ?? 0} rows` : 'Failed', state: sqlOk ? 'done' : 'failed' },
      { name: 'Result', sub: h?.complete ? 'Complete' : r.status === 'partial' ? 'Partial' : 'Not produced', state: h?.complete ? 'done' : 'failed' },
    ];
  }
  if (r.status === 'conversation') {
    return [
      { name: 'Question', sub: 'Received', state: 'done' },
      { name: 'Route', sub: 'Conversation (no tool used)', state: 'done' },
      { name: 'Result', sub: 'Reply returned', state: 'done' },
    ];
  }
  const sql = isSql(r) ? r.result : null;
  return [
    { name: 'Question', sub: 'Received', state: 'done' },
    { name: 'Route', sub: routed ? 'SQL Query Tool' : 'No tool selected', state: routed ? 'done' : 'failed' },
    { name: 'Generate SQL', sub: sql?.generation_method ? `Method: ${sql.generation_method}` : '—', state: tail() },
    { name: 'Validate', sub: 'Read-only checks', state: tail() },
    { name: 'Execute', sub: 'Read-only connection', state: tail() },
    { name: 'Result', sub: sql ? `${sql.row_count} ${sql.row_count === 1 ? 'row' : 'rows'}` : 'Not produced', state: ok ? 'done' : 'failed' },
  ];
}

function Pipeline({ r }: { r: AgentResponse | undefined }) {
  const steps = pipelineFor(r);
  return (
    <section className="card p-5" aria-label="Execution pipeline">
      <div className="eyebrow mb-4 tracking-[0.2em]">Execution pipeline</div>
      {steps.length === 0 ? (
        <p className="text-body-sm text-text-muted">Steps appear here after you ask a question.</p>
      ) : (
        <ol className="relative space-y-4 pl-6">
          <div className="absolute bottom-2 left-2.5 top-2 w-0.5 bg-lavender-light" aria-hidden="true" />
          {steps.map((s) => (
            <li key={s.name} className="relative flex items-center gap-3">
              <span className={`-ml-6 flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold shadow-sm ${s.state === 'done' ? 'bg-brand-blue text-on-primary' : s.state === 'failed' ? 'border-2 border-status-negative-text bg-surface-base' : 'border-2 border-lavender-light bg-surface-base'}`} aria-hidden="true">
                {s.state === 'done' && <Check size={12} />}
              </span>
              <div>
                <span className="text-label-md font-semibold text-text-primary">{s.name}</span>
                <span className="sr-only"> ({s.state})</span>
                <p className="text-body-sm text-text-muted">{s.sub}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function QueryDetails({ r }: { r: AgentResponse | undefined }) {
  const sql = r && isSql(r) ? r.result : null;
  const exp = r && isExposure(r) ? r.result : null;
  const hyb = r && isHybrid(r) ? r.result : null;
  const rows: [string, React.ReactNode][] = r
    ? [
        ['Status', r.status],
        ['Tool', r.selected_tool === 'sql_query' ? 'SQL Query Tool' : r.selected_tool === 'exposure_calculator' ? 'Exposure Calculator' : r.selected_tool === 'hybrid_analysis' ? 'Hybrid analysis (SQL + Exposure)' : r.selected_tool === 'scenario_analysis' ? 'Scenario analysis (deterministic, hypothetical)' : r.status === 'conversation' ? 'None (conversational reply)' : '—'],
        ['Route reason', r.route_reason],
        ...(sql
          ? ([
              ['Method', sql.generation_method ?? '—'],
              ...(sql.model_name ? [['Model', <code key="m" className="rounded bg-surface-container-low px-2 py-0.5 font-mono text-label-sm text-indigo-700">{sql.model_name}</code>] as [string, React.ReactNode]] : []),
              ['Fallback used', sql.fallback_used ? `true${sql.fallback_reason ? ` — ${sql.fallback_reason}` : ''}` : 'false'],
              ['Rows returned', sql.row_count],
              ...(sql.query_description ? [['Description (model-generated, not verified)', sql.query_description] as [string, React.ReactNode]] : []),
              ...(sql.answer_verification ? [['Answer verification', sql.answer_verification.status] as [string, React.ReactNode]] : []),
            ] as [string, React.ReactNode][])
          : []),
        ...(exp ? ([['Portfolio', exp.portfolio_name], ...(exp.answer_verification ? [['Answer verification', exp.answer_verification.status]] : [])] as [string, React.ReactNode][]) : []),
        ...(r && isScenario(r)
          ? ([
              ['Portfolio', r.result.portfolio_name ?? '—'],
              ['Scenario', r.result.scenario?.name ?? '—'],
              ['Verification', r.result.verification?.status ?? '—'],
              ['Answer verification', r.result.answer_verification?.status ?? '—'],
            ] as [string, React.ReactNode][])
          : []),
        ...(hyb
          ? ([
              ['Portfolio', hyb.portfolio?.portfolio_name ?? '—'],
              ['Complete', hyb.complete ? 'yes' : 'no (partial or failed)'],
              ['SQL component', hyb.component_status.sql],
              ...(hyb.components.sql?.generation_method ? [['Method', hyb.components.sql.generation_method] as [string, React.ReactNode]] : []),
              ['Exposure component', hyb.component_status.exposure],
              ['Verification', hyb.components.exposure?.verification?.status ?? '—'],
              ['Answer verification', hyb.answer_verification?.status ?? '—'],
            ] as [string, React.ReactNode][])
          : []),
      ]
    : [];
  return (
    <section className="card p-5" aria-label="Query details">
      <div className="eyebrow mb-4 tracking-[0.2em]">Query details</div>
      {!r ? (
        <p className="text-body-sm text-text-muted">Ask a question to inspect how it was routed and answered.</p>
      ) : (
        <dl className="space-y-2.5 text-body-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="flex flex-col gap-0.5 py-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
              <dt className="shrink-0 text-text-muted">{k}</dt>
              <dd className="font-medium text-text-primary sm:text-right">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

function Safeguards() {
  return (
    <section className="card p-5" aria-label="Safeguards">
      <div className="mb-3 flex items-center gap-2 text-indigo-800">
        <ShieldCheck size={20} aria-hidden="true" />
        <h2 className="text-label-md font-bold uppercase tracking-wider">Safeguards (always applied)</h2>
      </div>
      <ul className="space-y-2.5 pl-1 text-body-sm text-text-secondary">
        {['Only a single SELECT or WITH statement runs', 'Comments and write keywords are rejected', 'Read-only database connection', 'Literal values are passed as parameters'].map((t) => (
          <li key={t} className="flex items-start gap-2.5"><span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-700" aria-hidden="true" /><span>{t}</span></li>
        ))}
      </ul>
      <p className="mt-4 text-[11px] italic text-text-muted">Analytical result over a supplied sample dataset. Not investment advice.</p>
    </section>
  );
}

function EmptyState({ onSuggest }: { onSuggest: (q: string) => void }) {
  return (
    <div className="card relative my-auto overflow-hidden p-6 sm:p-8">
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-72 rotate-[-18deg] opacity-90" aria-hidden="true" style={{ background: 'linear-gradient(135deg,#4C4CCC,#0000A0)' }} />
      <p className="eyebrow mb-1">Welcome</p>
      <h2 className="mb-2 max-w-md text-headline-sm text-indigo-800">How can I help with your portfolios?</h2>
      <p className="mb-5 max-w-md text-body-md text-text-secondary">Ask in plain English. The agent chooses the right tool and shows you exactly how it answered.</p>
      <div className="mb-2 text-table-header uppercase tracking-[0.15em] text-text-muted">Try asking</div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Example questions">
        {SUGGESTIONS.map((s) => (
          <button key={s} type="button" onClick={() => onSuggest(s)} className="group inline-flex items-center gap-1.5 rounded-full bg-lavender-callout px-3.5 py-1.5 text-left text-label-md text-indigo-800 transition-colors hover:bg-lavender-input">
            <span>{s}</span>
            <ArrowUpRight size={14} className="shrink-0 text-indigo-700 transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" aria-hidden="true" />
          </button>
        ))}
      </div>
      <Link to="/evaluation" className="mt-5 inline-flex items-center gap-1.5 text-label-md text-violet-accent hover:text-brand-blue">
        <ListChecks size={16} aria-hidden="true" /> See how answers are checked against ground truth
      </Link>
    </div>
  );
}

const prefersReducedMotion = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const NEAR_BOTTOM_PX = 96;

export function Ask() {
  const [input, setInput] = useState('');
  const [thread, setThread] = useState<Exchange[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const nextId = useRef(1);
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Exchange to bring into view after the next render, and whether the reader is "following" the newest message.
  const pendingScroll = useRef<number | null>(null);
  const following = useRef(true);

  const onScroll = () => {
    const el = scrollRef.current;
    if (el) following.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  };

  // After the thread changes, scroll the flagged exchange (user bubble + its answer) to the top of the history.
  useLayoutEffect(() => {
    const id = pendingScroll.current;
    const box = scrollRef.current;
    if (id === null || !box) return;
    pendingScroll.current = null;
    const target = box.querySelector<HTMLElement>(`[data-exchange="${id}"]`);
    if (!target) return;
    const top = Math.max(0, target.offsetTop - 8);
    // Smooth for short hops; jump instantly across long distances (a slow glide over thousands of pixels feels laggy).
    const smooth = !prefersReducedMotion() && Math.abs(top - box.scrollTop) < box.clientHeight * 1.5;
    if (typeof box.scrollTo === 'function') box.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
    else box.scrollTop = top;
  }, [thread]);

  const run = useCallback(async (question: string, existingId?: number) => {
    const id = existingId ?? nextId.current++;
    // A new question always brings itself into view; a retry only does so if the reader is still at the bottom.
    if (!existingId || following.current) pendingScroll.current = id;
    following.current = true;
    setThread((t) => (existingId ? t.map((e) => (e.id === id ? { ...e, status: 'loading', networkError: undefined } : e)) : [...t, { id, question, status: 'loading' }]));
    setSelectedId(id);
    try {
      const response = await api.ask(question);
      // Keep the answer in view when it arrives, unless the reader has scrolled up to older messages meanwhile.
      if (following.current) pendingScroll.current = id;
      setThread((t) => t.map((e) => (e.id === id ? { ...e, status: 'done', response } : e)));
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'The request failed.';
      if (following.current) pendingScroll.current = id;
      setThread((t) => t.map((e) => (e.id === id ? { ...e, status: 'failed', networkError: message } : e)));
    }
  }, []);

  const busy = thread.some((e) => e.status === 'loading');
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput('');
    void run(q);
  };
  const suggest = (q: string) => {
    setInput(q);
    inputRef.current?.focus();
  };
  const selected = thread.find((e) => e.id === selectedId) ?? thread[thread.length - 1];
  const hasThread = thread.length > 0;

  return (
    <>
      <PageHeader
        dense
        crumb="Ask"
        eyebrow="Ask the agent"
        title="Ask a question about your portfolios"
        actions={
          hasThread ? (
            <button type="button" onClick={() => setInspectorOpen(true)} className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-lavender-callout px-3.5 py-2 text-label-md text-indigo-800 hover:bg-lavender-input lg:hidden">
              <PanelRight size={16} aria-hidden="true" /> Details
            </button>
          ) : undefined
        }
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-6 lg:grid-cols-12">
        <section aria-label="Conversation" className="flex min-h-0 flex-col lg:col-span-8">
          {/* Conversation history: scrolls on its own; oldest exchange first, newest last. */}
          <div ref={scrollRef} onScroll={onScroll} className="relative flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto overscroll-contain pb-4 pr-1 [scrollbar-gutter:stable]" role="log" aria-label="Conversation history" tabIndex={0}>
            {!hasThread && <EmptyState onSuggest={suggest} />}
            {thread.map((ex) => (
              <section key={ex.id} data-exchange={ex.id} aria-label={`Answer to: ${ex.question}`} className="shrink-0">
                <div className="mb-3 flex justify-end">
                  <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-lavender-input px-5 py-3 text-body-lg font-medium text-indigo-800 shadow-sm sm:max-w-[540px]">{ex.question}</div>
                </div>
                <AnswerCard
                  ex={ex}
                  selected={selected?.id === ex.id}
                  canSelect={thread.length > 1}
                  onSelect={() => { setSelectedId(ex.id); setInspectorOpen(true); }}
                  onRetry={() => void run(ex.question, ex.id)}
                  onSuggest={suggest}
                />
              </section>
            ))}
          </div>

          {/* Composer: pinned to the bottom of the chat column, with a soft fade over the history. */}
          <div className="relative -mx-4 shrink-0 border-t border-border-hairline bg-surface-base/95 px-4 pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-3 shadow-[0_-6px_16px_rgba(39,39,86,0.07)] backdrop-blur lg:mx-0 lg:rounded-t-2xl lg:px-5">
            <div className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-surface to-transparent" aria-hidden="true" />
            <form onSubmit={submit} className="relative flex items-center" role="search" aria-label="Ask a question">
              <Search size={22} className="pointer-events-none absolute left-4 text-indigo-700" aria-hidden="true" />
              <input
                ref={inputRef}
                aria-label="Your question"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                maxLength={500}
                enterKeyHint="send"
                autoComplete="off"
                placeholder="Ask about portfolios, holdings or sector exposure…"
                className="h-12 w-full rounded-full border border-lavender-light bg-surface pl-12 pr-28 text-body-md text-text-primary shadow-inner placeholder:text-text-muted focus:border-transparent focus:bg-surface-base focus:outline-none focus:ring-2 focus:ring-primary sm:h-14 sm:pr-32"
              />
              <button type="submit" disabled={busy || !input.trim()} className="absolute right-1.5 flex h-9 items-center gap-1.5 rounded-full px-4 text-label-lg tracking-wide text-on-primary shadow-sm transition-all hover:opacity-95 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50 sm:h-11 sm:px-6" style={{ background: 'linear-gradient(144deg, #4C4CCC 40.85%, #0000A0 98.66%)' }}>
                <span>Ask</span>
                <ArrowRight size={18} aria-hidden="true" />
              </button>
            </form>
            <p className="pt-1.5 text-center text-[11px] italic text-text-muted">Analytical result over a supplied sample dataset. Not investment advice.</p>
          </div>
        </section>

        {/* Inspector: a side column on desktop; on smaller screens a sheet opened from the "Details" button. */}
        <aside
          aria-label="Inspector"
          className={`${inspectorOpen ? 'fixed inset-0 z-[55] flex bg-surface p-4' : 'hidden'} min-h-0 flex-col gap-4 overflow-y-auto overscroll-contain lg:static lg:z-auto lg:col-span-4 lg:flex lg:bg-transparent lg:p-0 lg:pb-4 lg:pr-1`}
        >
          <div className="flex items-center justify-between lg:hidden">
            <h2 className="text-label-lg font-bold text-indigo-800">Answer details</h2>
            <button type="button" aria-label="Close details" onClick={() => setInspectorOpen(false)} className="rounded-full p-2 text-indigo-800 hover:bg-lavender-callout"><X size={22} aria-hidden="true" /></button>
          </div>
          <QueryDetails r={selected?.response} />
          <Pipeline r={selected?.response} />
          <Safeguards />
        </aside>
      </div>
    </>
  );
}
