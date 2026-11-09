import { Calculator, ListChecks, Ban, ShieldAlert, ShieldCheck, ShieldQuestion } from 'lucide-react';
import type { AnswerVerification, ExposureResult, ExposureVerification, Holding } from '../api/types';
import { colorFor, donutSegments, excludedFromExposurePct, largestStockBySector } from '../lib/exposure';
import { formatPercent } from '../lib/format';
import { BarRow, Donut, Legend } from './charts';
import { KpiTile, Pill } from './ui';

/**
 * Small status badge for the backend's independent verification. Three visibly different states:
 * verified (green: success), failed (red: figures disproved) and unverified (neutral: could not be checked).
 */
export function VerificationStatus({ verification }: { verification?: ExposureVerification }) {
  if (!verification) return null;
  const detail = `${verification.method} Tolerance ${verification.tolerance_pp} percentage points.`;
  if (verification.status === 'verified') {
    return <Pill tone="positive" icon={<ShieldCheck size={13} aria-hidden="true" />} title={detail}>Independently verified</Pill>;
  }
  if (verification.status === 'failed') {
    return <Pill tone="negative" icon={<ShieldAlert size={13} aria-hidden="true" />} title={detail}>Verification failed</Pill>;
  }
  return <Pill tone="neutral" icon={<ShieldQuestion size={13} aria-hidden="true" />} title={detail}>Could not be verified</Pill>;
}

/**
 * Badge for the final-answer verification (is the response consistent with the tool results?). Distinct states:
 * verified (green; "Partial" is neutral so a partial result is never styled as complete), failed (red),
 * could not be verified (neutral). Renders nothing when no analytical claim was verified.
 */
export function AnswerVerificationStatus({ verification, partial = false }: { verification?: AnswerVerification; partial?: boolean }) {
  if (!verification || verification.status === 'not_applicable') return null;
  const detail = `${verification.method} ${verification.unverified_claims.length ? 'Model-written text is not verified. ' : ''}Checks run: ${verification.checks_run}.`;
  if (verification.status === 'verified') {
    return partial
      ? <Pill tone="neutral" icon={<ShieldCheck size={13} aria-hidden="true" />} title={detail}>Partial · shown parts consistent</Pill>
      : <Pill tone="positive" icon={<ShieldCheck size={13} aria-hidden="true" />} title={detail}>Answer verified</Pill>;
  }
  if (verification.status === 'failed') {
    return <Pill tone="negative" icon={<ShieldAlert size={13} aria-hidden="true" />} title={verification.mismatches.map((m) => m.message).join(' ')}>Answer verification failed</Pill>;
  }
  return <Pill tone="neutral" icon={<ShieldQuestion size={13} aria-hidden="true" />} title={verification.error ?? detail}>Answer could not be verified</Pill>;
}

export function ExposureKpis({ result }: { result: ExposureResult }) {
  const excluded = excludedFromExposurePct(result.total_equity_weight_pct);
  return (
    <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
      <KpiTile
        label="Total equity weight"
        value={formatPercent(result.total_equity_weight_pct)}
        icon={<Calculator size={20} aria-hidden="true" />}
        caption={`SUM of ${result.equity_holdings_count} equity weights (not renormalised)`}
      />
      <KpiTile
        label="Equity holdings"
        value={result.equity_holdings_count}
        icon={<ListChecks size={20} aria-hidden="true" />}
        caption="Included in the sector exposure calculation"
      />
      <KpiTile
        label="Bond holdings excluded"
        value={result.excluded_bond_holdings}
        icon={<Ban size={20} aria-hidden="true" />}
        caption={excluded > 0 ? `${formatPercent(excluded)} of weight excluded from exposure` : 'No weight excluded from exposure'}
      />
    </div>
  );
}

export function ExposureCharts({ result, holdings }: { result: ExposureResult; holdings?: Holding[] }) {
  const segments = donutSegments(result);
  const largest = holdings ? largestStockBySector(holdings) : {};
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <section className="card flex flex-col justify-between p-6 lg:col-span-6" aria-label="Aggregate portfolio composition">
        <div className="flex items-center justify-between pb-4">
          <h3 className="text-headline-sm font-bold text-indigo-850">Aggregate Portfolio Composition</h3>
          <span className="rounded-full bg-surface-container px-2.5 py-0.5 text-label-sm text-text-muted">Actual weights</span>
        </div>
        <div className="my-4 flex flex-col items-center justify-around gap-6 sm:flex-row">
          <Donut segments={segments} centerValue={formatPercent(result.total_equity_weight_pct)} centerLabel="Equity weight" />
          <Legend segments={segments} />
        </div>
        {segments.some((s) => s.excluded) && (
          <p className="pt-4 text-label-sm text-text-secondary">
            “Excluded from exposure” is derived as 100% minus the total equity weight. Only stock weights are summed.
          </p>
        )}
      </section>
      <section className="card flex flex-col justify-between p-6 lg:col-span-6" aria-label="Sector exposures">
        <div>
          <div className="flex items-center justify-between pb-3">
            <h3 className="text-headline-sm font-bold text-indigo-850">Equity Exposure by Sector</h3>
            <span className="rounded-full bg-surface-container px-2.5 py-0.5 text-label-sm text-text-secondary">
              {result.equity_holdings_count} equity holdings
            </span>
          </div>
          <p className="mb-5 text-body-sm text-text-secondary">Equity weights summed as stored, without upward renormalisation.</p>
          <div className="flex flex-col gap-4">
            {result.exposures.map((e, i) => {
              const top = largest[e.sector];
              return (
                <BarRow
                  key={e.sector}
                  label={e.sector}
                  valueText={formatPercent(e.exposure_pct)}
                  pct={e.exposure_pct}
                  color={colorFor(i)}
                  caption={top ? `Largest holding: ${top.company_name} (${formatPercent(top.current_weight * 100)})` : undefined}
                />
              );
            })}
          </div>
        </div>
        <div className="mt-6 rounded-lg bg-lavender-callout p-4 text-body-sm text-indigo-700">
          <div className="mb-1 text-label-md font-bold text-indigo-900">Calculation standard</div>
          Exposure = SUM(current_weight) × 100 for Stock holdings. A mixed portfolio can total less than 100% because bonds are excluded.
        </div>
      </section>
    </div>
  );
}
