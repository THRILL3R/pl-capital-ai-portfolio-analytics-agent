import { ArrowRight, LockKeyhole } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, api } from '../api/client';

/**
 * The reviewer access screen. The code is typed into a password field, sent once to the server over the same origin and
 * then discarded: it is never kept in React state after submission and never written to localStorage or sessionStorage.
 * The session itself lives in an HttpOnly cookie that JavaScript cannot read.
 */
export function AccessScreen({ onSuccess }: { onSuccess: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!code || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.login(code);
      setCode('');
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not sign in. Please try again.');
      setCode('');
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface px-4 py-10">
      <div className="card w-full max-w-md p-8">
        <img src="/pl-capital-logo.svg" alt="PL Capital Prabhudas Lilladher" className="h-9 w-auto object-contain" />
        <p className="eyebrow mb-2 mt-8 flex items-center gap-2">
          <LockKeyhole size={14} aria-hidden="true" /> Reviewer access
        </p>
        <h1 className="text-headline-lg text-indigo-800">Enter your access code</h1>
        <p className="mt-3 text-body-md text-text-secondary">
          This demo of the Portfolio Analytics Agent is limited to reviewers. Use the access code provided with the submission.
        </p>

        <form onSubmit={submit} className="mt-6" aria-label="Reviewer access">
          <label htmlFor="access-code" className="mb-1.5 block text-label-lg font-semibold text-indigo-800">
            Access code
          </label>
          <input
            id="access-code"
            ref={inputRef}
            type="password"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            maxLength={256}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? 'access-error' : undefined}
            className="h-12 w-full rounded-full border border-lavender-light bg-surface px-5 text-body-md text-text-primary shadow-inner placeholder:text-text-muted focus:border-transparent focus:bg-surface-base focus:outline-none focus:ring-2 focus:ring-primary"
          />
          {error && (
            <p id="access-error" role="alert" className="mt-3 rounded-lg border border-status-negative-text/30 bg-status-negative-bg/50 px-3 py-2 text-body-sm text-text-primary">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !code}
            className="mt-5 inline-flex h-12 w-full items-center justify-center gap-2 rounded-full bg-primary-container px-6 text-label-lg tracking-wide text-on-primary shadow-sm transition-all hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span>{busy ? 'Checking…' : 'Continue'}</span>
            {!busy && <ArrowRight size={18} aria-hidden="true" />}
          </button>
        </form>

        <p className="mt-6 text-center text-[11px] italic text-text-muted">Sample assessment dataset. Analytics only — not investment advice.</p>
      </div>
    </main>
  );
}
