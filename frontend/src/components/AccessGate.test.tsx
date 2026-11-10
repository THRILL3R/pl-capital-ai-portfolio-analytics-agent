import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { AccessGate, useAuth } from './AccessGate';
import { Layout } from './Layout';

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;
let handler: Handler;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const SESSION_NONE = { auth_required: true, authenticated: false };
const SESSION_OK = { auth_required: true, authenticated: true };

function Protected() {
  const { authRequired, logout } = useAuth();
  return (
    <div>
      <p>Protected application</p>
      <p>{authRequired ? 'code required' : 'open'}</p>
      <button type="button" onClick={() => void logout()}>Sign out now</button>
    </div>
  );
}

const renderGate = (blockUntilChecked = true) => render(<AccessGate blockUntilChecked={blockUntilChecked}><Protected /></AccessGate>);

const typeCode = async (user: ReturnType<typeof userEvent.setup>, code: string) => {
  await user.type(screen.getByLabelText('Access code'), code);
  await user.click(screen.getByRole('button', { name: /Continue/ }));
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  handler = (url) => (url === '/api/auth/session' ? json(SESSION_NONE) : json({}));
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => handler(String(url), init)));
});

describe('AccessGate', () => {
  it('shows the application straight away when the server does not require a code', async () => {
    handler = (url) => (url === '/api/auth/session' ? json({ auth_required: false, authenticated: true }) : json({}));
    renderGate();
    expect(await screen.findByText('Protected application')).toBeInTheDocument();
    expect(screen.getByText('open')).toBeInTheDocument();
    expect(screen.queryByLabelText('Access code')).not.toBeInTheDocument();
  });

  it('shows nothing of the application, only the access screen, when a code is required and there is no session', async () => {
    renderGate();
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
    expect(screen.getByLabelText('Access code')).toHaveAttribute('type', 'password');
    expect(screen.queryByText('Protected application')).not.toBeInTheDocument();
  });

  it('shows no application content while the check is still running', () => {
    handler = () => new Promise<Response>(() => undefined);
    renderGate();
    expect(screen.getByRole('status', { name: 'Checking access' })).toBeInTheDocument();
    expect(screen.queryByText('Protected application')).not.toBeInTheDocument();
  });

  it('renders the application without waiting in development and test mode, and corrects itself', async () => {
    renderGate(false);
    expect(screen.getByText('Protected application')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
  });

  it('shows the application when a session already exists, with sign-out available', async () => {
    handler = (url) => (url === '/api/auth/session' ? json(SESSION_OK) : json({}));
    renderGate();
    expect(await screen.findByText('Protected application')).toBeInTheDocument();
    expect(screen.getByText('code required')).toBeInTheDocument();
  });

  it('rejects a wrong code, says so, clears the field and stays locked', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/login' ? json({ detail: 'Invalid access code.' }, 401) : json(SESSION_NONE));
    renderGate();
    await screen.findByLabelText('Access code');
    await typeCode(user, 'not-the-code-1234');
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid access code.');
    expect(screen.getByLabelText('Access code')).toHaveValue('');
    expect(screen.queryByText('Protected application')).not.toBeInTheDocument();
  });

  it('unlocks with the correct code and sends it once, as JSON, to the login endpoint only', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/login' ? json({ authenticated: true }) : json(SESSION_NONE));
    renderGate();
    await screen.findByLabelText('Access code');
    await typeCode(user, 'the-reviewer-code-5678');
    expect(await screen.findByText('Protected application')).toBeInTheDocument();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    const login = calls.filter(([url]) => url === '/api/auth/login');
    expect(login).toHaveLength(1);
    expect(login[0][1].method).toBe('POST');
    expect(JSON.parse(login[0][1].body as string)).toEqual({ code: 'the-reviewer-code-5678' });
    expect(login[0][1].credentials).toBe('same-origin');
    expect(calls.filter(([, init]) => String(init?.body ?? '').includes('the-reviewer-code-5678'))).toHaveLength(1);
  });

  it('never stores the code or any session value in localStorage or sessionStorage', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/login' ? json({ authenticated: true }) : json(SESSION_NONE));
    renderGate();
    await screen.findByLabelText('Access code');
    await typeCode(user, 'the-reviewer-code-5678');
    await screen.findByText('Protected application');
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).toBe('');   // the session cookie is HttpOnly: invisible to scripts (and never set by them)
  });

  it('shows the throttling message when the server limits login attempts', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/login' ? json({ detail: 'Too many attempts. Please try again later.' }, 429) : json(SESSION_NONE));
    renderGate();
    await screen.findByLabelText('Access code');
    await typeCode(user, 'whatever-code-9999');
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Please try again later.');
  });

  it('does not submit an empty code', async () => {
    renderGate();
    await screen.findByLabelText('Access code');
    expect(screen.getByRole('button', { name: /Continue/ })).toBeDisabled();
  });

  it('returns to the access screen when a data request is refused because the session expired', async () => {
    handler = (url) => (url === '/api/auth/session' ? json(SESSION_OK) : json({ detail: 'Authentication required.' }, 401));
    renderGate();
    await screen.findByText('Protected application');
    await expect(api.portfolios()).rejects.toMatchObject({ status: 401, message: 'Your session has expired. Please enter the access code again.' });
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
    expect(screen.queryByText('Protected application')).not.toBeInTheDocument();
  });

  it('signs out through the server and returns to the access screen', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/session' ? json(SESSION_OK) : url === '/api/auth/logout' ? json({ authenticated: false }) : json({}));
    renderGate();
    await user.click(await screen.findByRole('button', { name: 'Sign out now' }));
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    expect(calls.some(([url, init]) => url === '/api/auth/logout' && init?.method === 'POST')).toBe(true);
  });

  it('still returns to the access screen on sign-out if the server cannot be reached', async () => {
    const user = userEvent.setup();
    handler = (url) => {
      if (url === '/api/auth/session') return json(SESSION_OK);
      throw new TypeError('network');
    };
    renderGate();
    await user.click(await screen.findByRole('button', { name: 'Sign out now' }));
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
  });

  it('offers a retry when the server cannot be reached in a production build', async () => {
    const user = userEvent.setup();
    let reachable = false;
    handler = (url) => {
      if (!reachable) throw new TypeError('network');
      return url === '/api/auth/session' ? json(SESSION_OK) : json({});
    };
    renderGate();
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be reached');
    reachable = true;
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('Protected application')).toBeInTheDocument();
  });

  it('adds a Sign out button to the dashboard header only when a code is required', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/auth/session' ? json(SESSION_OK) : url === '/api/auth/logout' ? json({ authenticated: false }) : json({ status: 'ok' }));
    render(<MemoryRouter initialEntries={['/ask']}><AccessGate blockUntilChecked><Layout /></AccessGate></MemoryRouter>);
    const button = await screen.findByRole('button', { name: /Sign out/ });
    await user.click(button);
    expect(await screen.findByRole('heading', { name: 'Enter your access code' })).toBeInTheDocument();
  });

  it('does not show a Sign out button when no code is required', async () => {
    handler = (url) => (url === '/api/auth/session' ? json({ auth_required: false, authenticated: true }) : json({ status: 'ok' }));
    render(<MemoryRouter initialEntries={['/ask']}><AccessGate blockUntilChecked><Layout /></AccessGate></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Sign out/ })).not.toBeInTheDocument();
  });
});
