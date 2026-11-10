import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, onUnauthorized } from '../api/client';
import { AccessScreen } from './AccessScreen';
import { ErrorNotice, Skeleton } from './ui';

type Status = 'checking' | 'open' | 'locked' | 'error';

interface AuthState {
  /** True when the server requires a reviewer access code (false for a plain local run). */
  authRequired: boolean;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({ authRequired: false, logout: async () => undefined });

export const useAuth = () => useContext(AuthContext);

/**
 * Shows the reviewer access screen until the server confirms a session, and again whenever the session ends.
 *
 * The server decides: this component only asks (GET /api/auth/session) and reacts to 401 answers. In a production build it
 * shows nothing of the application until the check has finished, so there is no flash of protected content. In development and
 * in tests it renders the application straight away and corrects itself if the server says a code is required. When the
 * server does not require a code (a plain local run) the application is shown unchanged.
 */
export function AccessGate({ children, blockUntilChecked = import.meta.env.PROD }: { children: ReactNode; blockUntilChecked?: boolean }) {
  const [status, setStatus] = useState<Status>(blockUntilChecked ? 'checking' : 'open');
  const [authRequired, setAuthRequired] = useState(false);

  const check = useCallback(() => {
    let active = true;
    api
      .session()
      .then((session) => {
        if (!active) return;
        if (session?.auth_required) {
          setAuthRequired(true);
          setStatus(session.authenticated ? 'open' : 'locked');
        } else {
          setAuthRequired(false);
          setStatus('open');
        }
      })
      .catch(() => {
        if (active && blockUntilChecked) setStatus('error');
      });
    return () => {
      active = false;
    };
  }, [blockUntilChecked]);

  useEffect(() => check(), [check]);

  // Any data request the server refuses for lack of a session (for example after it expires) returns to the access screen.
  useEffect(
    () =>
      onUnauthorized(() => {
        setAuthRequired(true);
        setStatus('locked');
      }),
    [],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* the access screen is shown regardless; an unreachable server cannot end the session sooner than its expiry */
    } finally {
      setStatus('locked');
    }
  }, []);

  const value = useMemo(() => ({ authRequired, logout }), [authRequired, logout]);

  if (status === 'checking') {
    return (
      <div role="status" aria-label="Checking access" className="mx-auto max-w-md px-4 py-24">
        <Skeleton lines={3} />
      </div>
    );
  }
  if (status === 'error') {
    return (
      <main className="mx-auto max-w-xl px-4 py-24">
        <ErrorNotice
          message="The server could not be reached, so access could not be checked."
          onRetry={() => {
            setStatus('checking');
            check();
          }}
        />
      </main>
    );
  }
  if (status === 'locked') return <AccessScreen onSuccess={() => setStatus('open')} />;
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
