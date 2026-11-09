import { CheckCheck, Database, FileSearch, Terminal } from 'lucide-react';
import { useEffect, useState } from 'react';
import { ApiError, api } from '../api/client';
import type { EvalQuestion, EvalQuestionResult, Evaluation as EvaluationData, ReportMeta } from '../api/types';
import { Callout, Disclaimer, ErrorNotice, NoData, PageHeader, Pill, Skeleton } from '../components/ui';

function Result({ r, deterministicTool }: { r: EvalQuestionResult | null; deterministicTool?: boolean }) {
  if (!r) return <span className="text-text-muted">—</span>;
  return (
    <span className="inline-flex flex-col items-center gap-0.5">
      <Pill tone={r.result === 'PASS' ? 'positive' : 'negative'}>{r.result}</Pill>
      {deterministicTool && <span className="text-[10px] text-text-muted">deterministic tool</span>}
    </span>
  );
}

/** An earlier, superseded run: always neutral (never red/green) so an old failure cannot read as a current one. */
function HistoricalResult({ r }: { r: EvalQuestionResult | null }) {
  if (!r) return <span className="text-text-muted">—</span>;
  return (
    <span className="inline-flex flex-col items-center gap-0.5">
      <Pill tone="neutral">{r.result}</Pill>
      <span className="text-[10px] text-text-muted">earlier run</span>
    </span>
  );
}

function ToolPill({ tool }: { tool: string | undefined }) {
  if (!tool) return <span className="text-text-muted">—</span>;
  return tool === 'exposure_calculator'
    ? <Pill tone="violet">Exposure Calculator</Pill>
    : <Pill tone="neutral" icon={<Database size={14} aria-hidden="true" />}>SQL Query Tool</Pill>;
}

const when = (m: ReportMeta | null) => (m?.started ? new Date(m.started).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : null);

function ScoreTile({ label, meta, caption, empty }: { label: string; meta: ReportMeta | null; caption: React.ReactNode; empty: string }) {
  return (
    <div className="card flex flex-col justify-between p-5">
      <div className="mb-2 text-label-sm font-bold uppercase tracking-wider text-text-muted">{label}</div>
      {meta && meta.score !== null ? (
        <>
          <div className="tabular text-[40px] font-extrabold leading-tight tracking-tight text-violet-accent sm:text-[44px]">{meta.score} / {meta.total}</div>
          <p className="mt-3 rounded bg-surface-container-low/60 px-2 py-1.5 text-body-sm text-text-secondary">{caption}</p>
          {when(meta) && <p className="mt-2 text-label-sm text-text-muted">Run {when(meta)}</p>}
        </>
      ) : (
        <NoData title="No report on file" icon={<FileSearch size={28} aria-hidden="true" />}>{empty}</NoData>
      )}
    </div>
  );
}

const liveLabel = (m: ReportMeta | null) => (m?.role === 'final' ? 'Final live Gemini evaluation' : 'Latest full live Gemini run');

function Row({ q }: { q: EvalQuestion }) {
  const tool = q.live?.selected_tool ?? q.deterministic?.selected_tool;
  const failed = q.live?.result === 'FAIL';
  return (
    <tr className={failed ? 'bg-status-negative-bg/30' : 'transition-colors hover:bg-surface-container-low'}>
      <td className="px-4 py-3 text-center font-mono font-bold text-text-muted">Q{q.id}</td>
      <td className="px-4 py-3 font-medium text-indigo-900">“{q.question}”</td>
      <td className="px-4 py-3"><ToolPill tool={tool} /></td>
      <td className="px-4 py-3 text-center"><Result r={q.deterministic} /></td>
      <td className="px-4 py-3 text-center"><Result r={q.live} deterministicTool={q.live?.generation === 'deterministic'} /></td>
      <td className="px-4 py-3 text-center"><HistoricalResult r={q.historical_live ?? null} /></td>
      <td className="px-4 py-3 text-xs text-text-secondary">
        <div className="flex flex-col gap-0.5">
          {q.live_failures.map((f) => <span key={f}>{f}</span>)}
          {(q.historical_live_failures ?? []).map((f) => <span key={`h-${f}`} className="text-text-muted">Earlier run: {f}</span>)}
          {q.targeted && <span className="flex items-center gap-1 font-medium text-text-primary"><CheckCheck size={13} aria-hidden="true" /> Targeted re-test: {q.targeted.result}</span>}
          {!q.live_failures.length && !q.targeted && q.live && <span>Generation: {q.live.generation} · rows {q.live.rows}</span>}
        </div>
      </td>
    </tr>
  );
}

export function Evaluation() {
  const [data, setData] = useState<EvaluationData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    api.evaluation().then((d) => active && setData(d)).catch((e) => active && setError(e instanceof ApiError ? e.message : 'Could not load the evaluation.'));
    return () => { active = false; };
  }, []);

  const subsetIds = data?.questions.filter((q) => q.targeted).map((q) => `Q${q.id} ${q.targeted!.result}`) ?? [];
  const sql = data?.questions.filter((q) => q.type === 'text2sql').length ?? 0;
  const exp = (data?.questions.length ?? 0) - sql;

  return (
    <>
      <PageHeader crumb="Evaluation" eyebrow="Verification benchmark" title="Agent Evaluation Dashboard">
        <span className="text-body-sm text-text-muted">
          Static view built from the evaluator’s saved report files (<code className="rounded bg-surface-container px-1.5 py-0.5 font-mono text-indigo-900">evaluate.py</code>). There is no evaluation API.
        </span>
      </PageHeader>

      {error && <ErrorNotice message={error} />}
      {!error && !data && <div className="card p-6"><Skeleton lines={8} /></div>}
      {data && (
        <>
          <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
            <ScoreTile label="Deterministic evaluation" meta={data.deterministic} empty="Run: python evaluate.py --report docs/EVALUATION_DETERMINISTIC.md" caption="Rule-based SQL generation; no external LLM API." />
            <ScoreTile
              label={liveLabel(data.live)}
              meta={data.live}
              empty="Run: python evaluate.py --use-llm"
              caption={<>{data.live?.model && <code className="font-mono font-medium text-indigo-900">{data.live.model}</code>}{data.live_generation && <> · {data.live_generation.by_gemini} of {data.live_generation.sql_questions} SQL questions by Gemini · {data.live_generation.fallbacks} fallbacks</>}</>}
            />
            <ScoreTile
              label="Earlier live run (historical)"
              meta={data.historical_live ?? null}
              empty="No earlier live report is kept."
              caption={<>Superseded by the final live evaluation; kept for the record.{data.historical_live?.model && <> <code className="font-mono font-medium text-indigo-900">{data.historical_live.model}</code></>}</>}
            />
            <div className="card flex flex-col justify-between p-5">
              <div className="mb-2 text-label-sm font-bold uppercase tracking-wider text-text-muted">Targeted live re-test (historical)</div>
              {data.live_subset && subsetIds.length ? (
                <>
                  <div className="text-[34px] font-extrabold leading-tight text-violet-accent sm:text-[38px]">{subsetIds.join(' · ')}</div>
                  <p className="mt-3 rounded bg-surface-container-low/60 px-2 py-1.5 text-body-sm text-text-secondary">Subset run ({data.live_subset.total} {data.live_subset.total === 1 ? 'question' : 'questions'}), not a full evaluation.</p>
                  {when(data.live_subset) && <p className="mt-2 text-label-sm text-text-muted">Run {when(data.live_subset)}</p>}
                </>
              ) : (
                <NoData title="No targeted run on file" icon={<FileSearch size={28} aria-hidden="true" />}>Run: python evaluate.py --use-llm --questions 7</NoData>
              )}
            </div>
          </div>

          <div className="mb-6 flex items-center gap-3 rounded-xl bg-lavender-callout p-4">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface-base text-primary shadow-sm"><Terminal size={18} aria-hidden="true" /></span>
            <p className="text-body-sm text-indigo-900">
              Re-run from the command line: <code className="rounded bg-surface-base px-2 py-0.5 font-mono font-semibold text-indigo-850">python evaluate.py --use-llm</code>
            </p>
          </div>

          <section className="card overflow-hidden" aria-label="Query verification matrix">
            <div className="flex items-center justify-between bg-surface-table-header p-4">
              <h2 className="text-label-lg font-bold text-indigo-900">Query Verification Matrix <span className="font-normal text-text-muted">({data.questions.length} ground-truth questions)</span></h2>
            </div>
            <div className="overflow-x-auto" role="region" aria-label="Evaluation results" tabIndex={0}>
              <table className="w-full text-left text-table-cell">
                <thead>
                  <tr className="table-head">
                    <th scope="col" className="w-12 px-4 py-3 text-center">#</th>
                    <th scope="col" className="px-4 py-3">Question</th>
                    <th scope="col" className="px-4 py-3">Tool routed</th>
                    <th scope="col" className="px-4 py-3 text-center">Deterministic</th>
                    <th scope="col" className="px-4 py-3 text-center">Live Gemini (final)</th>
                    <th scope="col" className="px-4 py-3 text-center">Earlier live (historical)</th>
                    <th scope="col" className="px-4 py-3">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border-table-row">{data.questions.map((q) => <Row key={q.id} q={q} />)}</tbody>
              </table>
            </div>
            <div className="flex flex-col justify-between gap-3 bg-surface-container-low p-4 md:flex-row md:items-center">
              <div className="flex items-center gap-3 text-label-sm font-semibold uppercase tracking-wider text-text-muted">
                Test suite composition
                <div className="flex h-2 w-48 overflow-hidden rounded-full bg-surface-container-highest" role="img" aria-label={`${sql} SQL questions, ${exp} exposure questions`}>
                  <div className="bg-primary-container" style={{ width: `${(sql / Math.max(1, sql + exp)) * 100}%` }} />
                  <div className="bg-violet-accent" style={{ width: `${(exp / Math.max(1, sql + exp)) * 100}%` }} />
                </div>
              </div>
              <div className="flex items-center gap-4 text-xs text-text-secondary">
                <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full bg-primary-container" aria-hidden="true" />SQL questions ({sql})</span>
                <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full bg-violet-accent" aria-hidden="true" />Exposure questions ({exp})</span>
              </div>
            </div>
          </section>

          <div className="mt-6">
            <Callout title="Non-deterministic execution advisory">
              Live Gemini output is non-deterministic; each live figure is a single run. The final live run shows 10/10 and the earlier historical run 9/10, so live scores can vary between runs. The deterministic rule-based path reflects the supplied questions only.
            </Callout>
          </div>
          <p className="mt-6 text-label-sm text-text-muted">
            Sources: {[data.deterministic, data.live, data.historical_live, data.live_subset].filter(Boolean).map((m) => m!.source).join(' · ') || 'no report files found'}
          </p>
        </>
      )}
      <Disclaimer />
    </>
  );
}
