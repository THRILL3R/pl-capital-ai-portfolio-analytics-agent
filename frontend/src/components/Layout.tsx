import { LogOut, Menu, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { useHealth } from '../hooks/useHealth';
import { useAuth } from './AccessGate';

const NAV = [
  { to: '/ask', label: 'Ask' },
  { to: '/portfolios', label: 'Portfolios' },
  { to: '/exposure', label: 'Exposure' },
  { to: '/evaluation', label: 'Evaluation' },
  { to: '/about', label: 'About' },
];

function DatasetChip() {
  const health = useHealth();
  if (health.state === 'offline') {
    return (
      <span className="hidden items-center rounded-full border border-status-negative-text/40 bg-status-negative-bg px-3 py-1 text-label-md text-status-negative-text lg:inline-flex">
        Local server not reachable
      </span>
    );
  }
  const rows = health.state === 'ready' ? health.health.dataset_rows : null;
  return (
    <span className="hidden items-center rounded-full border border-lavender-light bg-lavender-callout px-3 py-1 text-label-md text-indigo-700 lg:inline-flex">
      {rows ? `Sample dataset · ${rows} rows` : 'Sample dataset'}
    </span>
  );
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const { authRequired, logout } = useAuth();
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);

  // The Ask screen is a fixed-height chat workspace: the page itself does not scroll, the conversation does.
  const chat = location.pathname.startsWith('/ask');
  const [viewportH, setViewportH] = useState<number | null>(null);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!chat || !vv) return;
    // Track the visual viewport so an on-screen keyboard (iOS Safari does not resize the layout viewport)
    // shrinks the workspace instead of covering the composer.
    const update = () => {
      setViewportH(Math.round(vv.height));
      if (vv.offsetTop > 0) window.scrollTo(0, 0);
    };
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
    };
  }, [chat]);

  return (
    <div className={chat ? 'flex h-dvh flex-col overflow-hidden' : 'flex min-h-screen flex-col'} style={chat && viewportH ? { height: viewportH } : undefined}>
      <header className="sticky top-0 z-50 border-b border-border-hairline bg-surface-base shadow-[0_2px_8px_rgba(39,39,86,0.06)]">
        <div className="mx-auto flex h-[60px] max-w-[1200px] items-center justify-between gap-4 px-4 md:h-[68px] md:gap-6">
          <Link to="/ask" className="flex shrink-0 items-center gap-3" aria-label="PL Capital Portfolio Analytics Agent, home">
            <img src="/pl-capital-logo.svg" alt="PL Capital Prabhudas Lilladher" className="h-8 w-auto object-contain" />
            <span className="hidden h-6 w-px bg-border-hairline lg:block" aria-hidden="true" />
            <span className="hidden text-[15px] font-semibold tracking-tight text-indigo-800 lg:inline">Portfolio Analytics Agent</span>
          </Link>

          <nav aria-label="Primary" className="hidden h-full items-center gap-5 md:flex lg:gap-6">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) =>
                  `inline-flex h-full items-center border-b-2 px-1 text-label-lg transition-colors ${isActive ? 'border-brand-blue font-semibold text-brand-blue' : 'border-transparent text-indigo-800 hover:text-brand-blue'}`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex shrink-0 items-center gap-4">
            <DatasetChip />
            {authRequired && (
              <button
                type="button"
                onClick={() => void logout()}
                className="hidden items-center gap-1.5 rounded-full border border-lavender-light px-3 py-1 text-label-md text-indigo-700 hover:bg-lavender-callout md:inline-flex"
              >
                <LogOut size={14} aria-hidden="true" /> Sign out
              </button>
            )}
            <Link to="/about#how-it-works" className="hidden text-label-md text-violet-accent hover:text-primary lg:inline">
              How it works
            </Link>
            <button
              type="button"
              className="rounded-full p-2 text-indigo-800 hover:bg-lavender-callout md:hidden"
              aria-label="Open menu"
              aria-expanded={open}
              aria-controls="mobile-menu"
              onClick={() => setOpen(true)}
            >
              <Menu size={24} />
            </button>
          </div>
        </div>
        <div className="border-y border-border-hairline bg-lavender-callout px-4 py-1.5">
          <p className="text-center text-body-sm font-medium text-text-secondary">
            Sample assessment dataset (US-listed, USD). Analytics only — not investment advice.
          </p>
        </div>
      </header>

      {open && (
        <div id="mobile-menu" role="dialog" aria-modal="true" aria-label="Menu" className="fixed inset-0 z-[60] overflow-y-auto bg-surface-base px-6 py-5 md:hidden">
          <div className="mb-6 flex items-center justify-between">
            <span className="text-body-lg text-indigo-800">Menu</span>
            <button type="button" aria-label="Close menu" className="rounded-full p-2 text-indigo-800 hover:bg-lavender-callout" onClick={() => setOpen(false)}>
              <X size={26} />
            </button>
          </div>
          <ul>
            {NAV.map((n) => (
              <li key={n.to} className="border-b border-lavender-light/60">
                <NavLink
                  to={n.to}
                  className={({ isActive }) => `flex min-h-[56px] items-center text-[24px] font-semibold leading-[44px] ${isActive ? 'text-brand-blue' : 'text-indigo-800'}`}
                >
                  {n.label}
                </NavLink>
              </li>
            ))}
          </ul>
          {authRequired && (
            <button
              type="button"
              onClick={() => void logout()}
              className="mt-6 inline-flex min-h-[48px] items-center gap-2 rounded-full border border-lavender-light px-5 text-label-lg text-indigo-700 hover:bg-lavender-callout"
            >
              <LogOut size={18} aria-hidden="true" /> Sign out
            </button>
          )}
        </div>
      )}

      <main className={chat ? 'mx-auto flex min-h-0 w-full max-w-[1200px] flex-1 flex-col px-4 pt-4 md:pt-5' : 'mx-auto w-full max-w-[1200px] flex-1 px-4 py-6'}>
        <Outlet />
      </main>

      {!chat && (
        <footer className="border-t border-border-hairline bg-surface-base px-4 py-4 sm:px-8">
          <p className="text-center text-body-sm text-text-muted">
            Candidate-prepared assessment prototype · Sample data, not PL Capital client data · Version 1.0
          </p>
        </footer>
      )}
    </div>
  );
}
