import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import type { AgentResponse, Evaluation as EvalData, PortfolioDetail, PortfolioSummary } from '../api/types';

type Handler = (url: string, init?: RequestInit) => unknown | Promise<unknown>;
let handler: Handler;

function mockFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const body = await handler(String(url), init);
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
}

const HEALTH = { status: 'ok', database_ready: true, dataset_rows: 603, llm_enabled: false, gemini_configured: false };

const sqlResponse = (over: Partial<AgentResponse['result']> = {}, top: Partial<AgentResponse> = {}): AgentResponse => ({
  status: 'success',
  question: 'q',
  selected_tool: 'sql_query',
  route_reason: 'Question routed to SQL query tool for database analysis.',
  error: null,
  result: {
    status: 'success', tool: 'sql_query', original_question: 'q', generated_sql: 'SELECT symbol, cost_basis FROM holdings', query_parameters: ['Growth Equity Fund'],
    row_count: 2, columns: ['symbol', 'cost_basis'], rows: [{ symbol: 'AAPL', cost_basis: 438000 }, { symbol: 'AMZN', cost_basis: 320000 }],
    generation_method: 'rule_based', model_name: null, fallback_used: false, fallback_reason: null, query_description: null, error: null, ...over,
  } as never,
  ...top,
});

const renderAt = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

beforeEach(() => {
  handler = (url) => (url === '/api/health' ? HEALTH : {});
  mockFetch();
});

describe('shell and navigation', () => {
  it('renders the five primary routes and the real dataset row count', async () => {
    handler = (url) => (url === '/api/health' ? HEALTH : {});
    renderAt('/ask');
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    for (const label of ['Ask', 'Portfolios', 'Exposure', 'Evaluation', 'About']) expect(within(nav).getByRole('link', { name: label })).toBeInTheDocument();
    expect(await screen.findByText('Sample dataset · 603 rows')).toBeInTheDocument();
    expect(screen.getByText(/Sample assessment dataset \(US-listed, USD\)/)).toBeInTheDocument();
  });

  it('says so when the local server is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network'); }));
    renderAt('/ask');
    expect(await screen.findByText('Local server not reachable')).toBeInTheDocument();
  });

  it('opens and closes the mobile menu', async () => {
    const user = userEvent.setup();
    renderAt('/ask');
    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(screen.getByRole('dialog', { name: 'Menu' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('dialog', { name: 'Menu' })).not.toBeInTheDocument();
  });
});

describe('Ask chat layout', () => {
  const ask = async (user: ReturnType<typeof userEvent.setup>, q: string) => {
    await user.type(screen.getByLabelText('Your question'), q);
    await user.click(screen.getByRole('button', { name: 'Ask' }));
  };
  const answerFor = (q: string): AgentResponse =>
    q === 'hi'
      ? { status: 'conversation', question: q, selected_tool: null, route_reason: 'greeting', error: null, result: { tool: 'conversation', intent: 'greeting', message: 'Hello there', suggested_questions: [] } as never }
      : sqlResponse({ original_question: q, query_description: `Answer to ${q}` } as never, { question: q });

  it('shows a welcome with example chips and the composer below it, then removes the welcome once chatting', async () => {
    const user = userEvent.setup();
    handler = (url, init) => (url === '/api/health' ? HEALTH : answerFor(JSON.parse(String(init?.body)).question));
    renderAt('/ask');
    const welcome = screen.getByText('How can I help with your portfolios?');
    const log = screen.getByRole('log', { name: 'Conversation history' });
    const composer = screen.getByRole('search', { name: 'Ask a question' });
    expect(log).toContainElement(welcome);
    expect(within(screen.getByRole('group', { name: 'Example questions' })).getAllByRole('button')).toHaveLength(5);
    // The composer comes after the history in the document (and is not inside the scrolling log).
    expect(log.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(log).not.toContainElement(composer);
    await ask(user, 'hi');
    await screen.findByText('Hello there');
    expect(screen.queryByText('How can I help with your portfolios?')).not.toBeInTheDocument();
  });

  it('lists exchanges oldest first, each with the question bubble above its answer', async () => {
    const user = userEvent.setup();
    handler = (url, init) => (url === '/api/health' ? HEALTH : answerFor(JSON.parse(String(init?.body)).question));
    renderAt('/ask');
    for (const q of ['hi', 'first portfolio question', 'second portfolio question']) {
      await ask(user, q);
      await screen.findByRole('region', { name: `Answer to: ${q}` });
      await waitFor(() => expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()); // input cleared, ready again
    }
    const log = screen.getByRole('log', { name: 'Conversation history' });
    const sections = Array.from(log.querySelectorAll('section[data-exchange]'));
    expect(sections.map((s) => s.getAttribute('aria-label'))).toEqual(['Answer to: hi', 'Answer to: first portfolio question', 'Answer to: second portfolio question']);
    for (const s of sections) {
      const bubble = s.firstElementChild as HTMLElement;
      const card = s.lastElementChild as HTMLElement;
      expect(bubble.textContent).toBe((s.getAttribute('aria-label') ?? '').replace('Answer to: ', ''));
      expect(card.className).toContain('card');
      expect(bubble.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    // The inspector follows the newest exchange.
    expect(within(screen.getByRole('region', { name: 'Query details' })).getByText('Answer to second portfolio question')).toBeInTheDocument();
  });

  it('shows a loading state for the newest exchange only, below the earlier ones', async () => {
    const user = userEvent.setup();
    let release: (r: AgentResponse) => void = () => undefined;
    let calls = 0;
    handler = (url, init) => {
      if (url === '/api/health') return HEALTH;
      const q = JSON.parse(String(init?.body)).question;
      return ++calls === 1 ? answerFor(q) : new Promise<AgentResponse>((resolve) => { release = resolve; });
    };
    renderAt('/ask');
    await ask(user, 'hi');
    await screen.findByText('Hello there');
    await ask(user, 'slow question');
    const sections = await waitFor(() => {
      const s = Array.from(screen.getByRole('log', { name: 'Conversation history' }).querySelectorAll('section[data-exchange]'));
      expect(s).toHaveLength(2);
      return s;
    });
    expect(sections[1].textContent).toContain('slow question');
    expect(sections[1].querySelector('[class*="animate-pulse"]')).not.toBeNull();
    expect(sections[0].querySelector('[class*="animate-pulse"]')).toBeNull();
    release(answerFor('slow question'));
    expect((await screen.findAllByText('Answer to slow question')).length).toBeGreaterThan(0);
  });

  it('scrolls to a new question, and keeps a finished answer in view only if the reader is still following', async () => {
    const user = userEvent.setup();
    const scrollTo = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: scrollTo });
    let release: (r: AgentResponse) => void = () => undefined;
    handler = (url) => (url === '/api/health' ? HEALTH : new Promise<AgentResponse>((resolve) => { release = resolve; }));
    renderAt('/ask');
    await ask(user, 'q1');
    await waitFor(() => expect(scrollTo).toHaveBeenCalledTimes(1)); // submitted: newest exchange brought into view
    // The reader scrolls up to older content while the answer is still loading.
    const log = screen.getByRole('log', { name: 'Conversation history' });
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 2000 });
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 400 });
    log.scrollTop = 0;
    log.dispatchEvent(new Event('scroll'));
    release(sqlResponse({ query_description: 'Finished answer' }, { question: 'q1' }));
    expect((await screen.findAllByText('Finished answer')).length).toBeGreaterThan(0);
    expect(scrollTo).toHaveBeenCalledTimes(1); // no jump back to the bottom
    // Reader returns to the bottom, then asks again: it scrolls again.
    log.scrollTop = 1700;
    log.dispatchEvent(new Event('scroll'));
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse({ query_description: 'Second answer' }, { question: 'q2' }));
    await ask(user, 'q2');
    expect((await screen.findAllByText('Second answer')).length).toBeGreaterThan(0);
    expect(scrollTo.mock.calls.length).toBeGreaterThanOrEqual(2);
    // Asking a new question always scrolls, even if the reader was reading older messages.
    const before = scrollTo.mock.calls.length;
    log.scrollTop = 0;
    log.dispatchEvent(new Event('scroll'));
    await ask(user, 'q3');
    await screen.findByRole('region', { name: 'Answer to: q3' });
    expect(scrollTo.mock.calls.length).toBeGreaterThan(before);
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollTo;
  });

  it('opens the answer details sheet from the Details button on small screens', async () => {
    const user = userEvent.setup();
    handler = (url, init) => (url === '/api/health' ? HEALTH : answerFor(JSON.parse(String(init?.body)).question));
    renderAt('/ask');
    expect(screen.queryByRole('button', { name: /Details/ })).not.toBeInTheDocument(); // nothing to inspect yet
    await ask(user, 'q1');
    await screen.findByRole('region', { name: 'Answer to: q1' });
    await user.click(screen.getByRole('button', { name: /Details/ }));
    expect(screen.getByRole('complementary', { name: 'Inspector' }).className).toContain('fixed');
    await user.click(screen.getByRole('button', { name: 'Close details' }));
    expect(screen.getByRole('complementary', { name: 'Inspector' }).className).toContain('hidden');
  });
});

describe('Ask', () => {
  it('shows the real agent response fields and never invents timing or P&L', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse());
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'top holdings');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('SUCCESS')).toBeInTheDocument();
    const details = screen.getByRole('region', { name: 'Query details' });
    expect(within(details).getByText('Route reason')).toBeInTheDocument();
    expect(within(details).getByText('Question routed to SQL query tool for database analysis.')).toBeInTheDocument();
    expect(within(details).getByText('rule_based')).toBeInTheDocument();
    expect(screen.getByText('$438,000')).toBeInTheDocument();
    expect(screen.getByText('param_1: "Growth Equity Fund"')).toBeInTheDocument();
    const text = document.body.textContent ?? '';
    for (const forbidden of [/\bms\b/, /Executed in/, /AST parsed/, /Unrealized/i, /Weighted Return/i, /Run hash/i]) expect(text).not.toMatch(forbidden);
  });

  it('displays formatted, highlighted SQL but copies the original executable SQL and keeps parameters', async () => {
    const user = userEvent.setup(); // installs a clipboard stub on navigator
    const original = 'SELECT t1.symbol, t1.cost_basis FROM holdings AS t1 JOIN portfolios AS t2 ON t1.portfolio_id = t2.portfolio_id WHERE t2.portfolio_name = ? ORDER BY t1.cost_basis DESC LIMIT ?';
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse({ generated_sql: original, query_parameters: ['Growth Equity Fund', 5] }));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'top holdings');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const sqlBox = await screen.findByRole('region', { name: 'Generated SQL, formatted' });

    // Display: one clause / column / JOIN per line, ON indented under its JOIN, 4-space steps.
    const rendered = Array.from(sqlBox.querySelectorAll('code > span')).map((l) => l.textContent ?? '');
    expect(rendered.length).toBeGreaterThan(8);
    expect(rendered).toContain('FROM');
    expect(rendered).toContain('JOIN portfolios AS t2');
    expect(rendered).toContain('ON t1.portfolio_id = t2.portfolio_id');
    expect(rendered[rendered.length - 1]).toBe('LIMIT ?');
    const rows = Array.from(sqlBox.querySelectorAll<HTMLElement>('code > span'));
    const padding = (text: string) => rows.find((r) => r.textContent === text)!.style.paddingLeft;
    expect(padding('SELECT')).toBe('4ch'); // indent 0 + 4ch hanging
    expect(padding('t1.symbol,')).toBe('8ch'); // indent 4
    expect(padding('ON t1.portfolio_id = t2.portfolio_id')).toBe('12ch'); // indent 8
    expect(rows[0].style.textIndent).toBe('-4ch');

    // Highlighting: keyword vs table vs placeholder with its bound value.
    expect(within(sqlBox).getAllByText('SELECT')[0].className).toContain('text-brand-blue');
    expect(within(sqlBox).getByText('holdings').className).toContain('text-indigo-900');
    const placeholders = within(sqlBox).getAllByText('?');
    expect(placeholders.map((p) => p.title)).toEqual(['Bound parameter 1: "Growth Equity Fund"', 'Bound parameter 2: 5']);

    // Copy: the original single-line statement, byte for byte (not the formatted text).
    await user.click(screen.getByRole('button', { name: /Copy SQL/ }));
    expect(await navigator.clipboard.readText()).toBe(original);
    expect(await screen.findByText('Copied')).toBeInTheDocument();

    // Bound parameters are still listed separately.
    expect(screen.getByText('param_1: "Growth Equity Fund"')).toBeInTheDocument();
    expect(screen.getByText('param_2: 5')).toBeInTheDocument();
  });

  const EXAMPLES = ['How many portfolios do we have in total?', 'What are the names of all active portfolios?', 'Show me the top 5 holdings by cost basis in the Growth Equity Fund', 'What are the sector exposures for the Tech Innovation Fund?'];
  const conversation = (intent: string, suggested: string[]): AgentResponse => ({
    status: 'conversation', question: 'hi', selected_tool: null, route_reason: `Recognised a conversational ${intent}; answered directly without invoking any tool.`, error: null,
    result: { tool: 'conversation', intent, message: 'Hello! I can answer questions about portfolios.', suggested_questions: suggested } as never,
  });

  it('renders a greeting as a friendly reply with clickable examples, not as an unsupported question', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : conversation('greeting', EXAMPLES));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'hi');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('Hello! I can answer questions about portfolios.')).toBeInTheDocument();
    expect(screen.queryByText('This question is not supported')).not.toBeInTheDocument();
    expect(screen.queryByText(/Question not recognized/)).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Generated SQL, formatted' })).not.toBeInTheDocument();
    const group = screen.getByRole('group', { name: 'Example questions' });
    expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(EXAMPLES);
    const details = screen.getByRole('region', { name: 'Query details' });
    expect(within(details).getByText('None (conversational reply)')).toBeInTheDocument();
    expect(screen.queryByText('SQL Query Tool')).not.toBeInTheDocument();
    // Clicking an example puts it in the question box.
    await user.click(within(group).getByRole('button', { name: EXAMPLES[0] }));
    expect(screen.getByLabelText('Your question')).toHaveValue(EXAMPLES[0]);
  });

  const convo = (intent: string, message: string, extra: Record<string, unknown> = {}): AgentResponse => ({
    status: 'conversation', question: 'q', selected_tool: null, error: null,
    route_reason: `Recognised a conversational ${intent} (local semantic classifier); answered directly without invoking any tool.`,
    result: { tool: 'conversation', intent, message, suggested_questions: EXAMPLES, ...extra } as never,
  });

  it('renders an identity reply as a chat message with examples, with no tool, SQL or score shown', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : convo('identity', 'I am the PL Capital Portfolio Analytics Agent, a prototype assistant.'));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'who are you');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText(/I am the PL Capital Portfolio Analytics Agent/)).toBeInTheDocument();
    expect(screen.queryByText('SQL Query Tool')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Generated SQL, formatted' })).not.toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: 'Example questions' })).getAllByRole('button')).toHaveLength(4);
    expect(document.body.textContent ?? '').not.toMatch(/similarity|confidence|score/i);
  });

  it('renders the capabilities reply with the implemented-feature list and clickable examples', async () => {
    const user = userEvent.setup();
    const details = ['Portfolio facts: how many portfolios there are.', 'Sector exposure: a deterministic breakdown for a named portfolio.', 'This instance uses the rule-based SQL generator only.'];
    handler = (url) => (url === '/api/health' ? HEALTH : convo('capabilities', 'Here is what I can do:', { details }));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'what can you do');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    const list = await screen.findByRole('list', { name: 'What I can do' });
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(details);
    await user.click(within(screen.getByRole('group', { name: 'Example questions' })).getByRole('button', { name: EXAMPLES[2] }));
    expect(screen.getByLabelText('Your question')).toHaveValue(EXAMPLES[2]);
  });

  it('renders a clarification request with examples and never as an error or unsupported notice', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : convo('clarification', 'I am not sure what you would like to know. Could you be more specific?'));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'tell me more');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText(/Could you be more specific/)).toBeInTheDocument();
    expect(screen.queryByText('This question is not supported')).not.toBeInTheDocument();
    expect(screen.queryByText('ERROR')).not.toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: 'Example questions' })).getAllByRole('button')).toHaveLength(4);
  });

  it('shows a locally rejected off-topic question as unsupported, with suggestions and no tool badge', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : {
      status: 'unsupported', question: 'weather', selected_tool: null, result: null, error: "That's outside what I can help with.",
      route_reason: 'Recognised an out-of-scope question (local semantic classifier); declined without invoking any tool.',
    });
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'weather');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('This question is not supported')).toBeInTheDocument();
    expect(screen.getByText("That's outside what I can help with.")).toBeInTheDocument();
    expect(screen.queryByText('SQL Query Tool')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: EXAMPLES[0] }).length).toBeGreaterThan(0);
  });

  it('renders a goodbye without example buttons', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : conversation('goodbye', []));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'bye');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('Hello! I can answer questions about portfolios.')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Example questions' })).not.toBeInTheDocument();
  });

  it('still shows the unsupported-question notice for questions the agent cannot answer', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : {
      status: 'unsupported', question: 'weather', selected_tool: 'sql_query', route_reason: 'Question routed to SQL query tool for database analysis.',
      error: 'Question not recognized by rule-based SQL generator.', result: null,
    });
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'weather');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('This question is not supported')).toBeInTheDocument();
    expect(screen.getByText('Question not recognized by rule-based SQL generator.')).toBeInTheDocument();
  });

  it('labels a Gemini fallback as a fallback, with the reason', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse({ fallback_used: true, fallback_reason: 'Gemini timed out', generation_method: 'rule_based' }));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'x');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByTitle('Gemini timed out')).toHaveTextContent('Fallback used');
    expect(screen.getByText(/true — Gemini timed out/)).toBeInTheDocument();
    expect(screen.queryByText('Gemini')).not.toBeInTheDocument();
  });

  it('shows the model only when the backend returns one', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse({ generation_method: 'gemini', model_name: 'gemini-3.5-flash-lite', query_description: 'Top holdings' }));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'x');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('gemini-3.5-flash-lite')).toBeInTheDocument();
    expect(screen.getByText('Top holdings', { selector: 'p' })).toBeInTheDocument();
  });

  it('renders a single value as a KPI numeral', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : sqlResponse({ columns: ['COUNT(*)'], rows: [{ 'COUNT(*)': 13 }], row_count: 1 }));
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'x');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('13')).toBeInTheDocument();
  });

  it('handles unsupported questions with recovery suggestions', async () => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : { status: 'unsupported', question: 'q', selected_tool: 'sql_query', route_reason: 'r', result: null, error: 'Unsupported question.' } satisfies AgentResponse);
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'weather?');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('This question is not supported')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'How many portfolios do we have in total?' }).length).toBeGreaterThan(0);
  });

  it('shows a retryable error when the request fails', async () => {
    const user = userEvent.setup();
    let calls = 0;
    handler = (url) => {
      if (url === '/api/health') return HEALTH;
      calls += 1;
      if (calls === 1) return new Response(JSON.stringify({ detail: 'Internal error.' }), { status: 500 });
      return sqlResponse();
    };
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'x');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Internal error.');
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('SUCCESS')).toBeInTheDocument();
  });

  it('does not submit an empty question', async () => {
    renderAt('/ask');
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled();
  });
});

const PORTFOLIOS: PortfolioSummary[] = [
  { portfolio_id: 1, portfolio_name: 'Growth Equity Fund', strategy_type: 'Growth', target_risk_level: 'High', status: 'Active', total_aum: 25000000 },
  { portfolio_id: 2, portfolio_name: 'ESG Sustainable Fund', strategy_type: 'ESG', target_risk_level: 'Medium', status: 'Active', total_aum: 15000000 },
  { portfolio_id: 3, portfolio_name: 'Total Bond Market Index Fund', strategy_type: 'Index', target_risk_level: 'Low', status: 'Passive', total_aum: 30000000 },
];
const detail = (p: PortfolioSummary, withSeries: boolean): PortfolioDetail => ({
  portfolio: p,
  holdings: [{ symbol: 'AAPL', company_name: 'Apple Inc.', asset_type: 'Stock', sector: 'Technology', quantity: 2500, cost_basis: 438000, current_price: 185.5, current_weight: 0.18 }],
  performance: withSeries ? [{ performance_date: '2023-09-25', nav: 125.45, total_return_1m: 2.1, total_return_3m: null, total_return_6m: null, total_return_1y: 22.8, volatility: 18.5, sharpe_ratio: 1.23, max_drawdown: -8.2 }, { performance_date: '2023-09-26', nav: 125.89, total_return_1m: 2.2, total_return_3m: null, total_return_6m: null, total_return_1y: 23.1, volatility: 18.4, sharpe_ratio: 1.24, max_drawdown: -8.1 }] : [],
  risk: withSeries ? [{ calculation_date: '2023-09-25', var_95: -4.2, var_99: -6.8, cvar_95: -5.1, beta: 1.15, correlation_sp500: 0.89, tracking_error: 4.2, information_ratio: null, sortino_ratio: null }] : [],
});

describe('Portfolios', () => {
  beforeEach(() => {
    handler = (url) => {
      if (url === '/api/health') return HEALTH;
      if (url === '/api/portfolios') return PORTFOLIOS;
      const m = url.match(/^\/api\/portfolios\/(\d+)$/);
      if (m) return detail(PORTFOLIOS[Number(m[1]) - 1], m[1] === '1');
      return {};
    };
  });

  it('lists the portfolios with counts and the AUM total computed from the rows', async () => {
    renderAt('/portfolios');
    expect(await screen.findByRole('button', { name: 'All (3)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Active (2)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Passive (1)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'High risk (1)' })).toBeInTheDocument();
    expect(screen.getAllByText('$70,000,000').length).toBeGreaterThan(0);
    expect(screen.queryByText(/benchmark/i, { selector: 'th' })).not.toBeInTheDocument();
  });

  it('filters rows', async () => {
    const user = userEvent.setup();
    renderAt('/portfolios');
    await user.click(await screen.findByRole('button', { name: 'Passive (1)' }));
    const table = screen.getByRole('region', { name: 'Portfolios' });
    expect(within(table).getByText('Total Bond Market Index Fund')).toBeInTheDocument();
    expect(within(table).queryByText('Growth Equity Fund')).not.toBeInTheDocument();
  });

  it('shows real series when records exist and "No data available" when they do not', async () => {
    const user = userEvent.setup();
    renderAt('/portfolios');
    await screen.findByRole('tablist');
    await user.click(await screen.findByRole('tab', { name: 'Performance' }));
    expect(await screen.findByText('2 dated records')).toBeInTheDocument();
    expect(screen.getByText(/Values are shown exactly as stored/)).toBeInTheDocument();

    await user.click(within(screen.getByRole('region', { name: 'Portfolios' })).getByText('ESG Sustainable Fund'));
    await user.click(screen.getByRole('tab', { name: 'Performance' }));
    expect(await screen.findByText('No data available')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Risk' }));
    expect(await screen.findByText('No data available')).toBeInTheDocument();
  });
});

describe('Evaluation', () => {
  const meta = (source: string, score: number, total: number, extra = {}) => ({ source, mode: 'm', score, total, model: null, started: '2026-11-09T04:03:07+05:30', ...extra });
  const result = (r: 'PASS' | 'FAIL', generation = 'gemini') => ({ type: 'text2sql', expected_tool: 'sql_query', selected_tool: 'sql_query', generation, rows: '1/1', result: r });
  const DATA: EvalData = {
    deterministic: meta('docs/EVALUATION_DETERMINISTIC.md', 10, 10),
    live: meta('docs/EVALUATION_FINAL_LIVE_20261109.md', 10, 10, { model: 'gemini-3.5-flash-lite', role: 'final', started: '2026-11-09T20:26:20+05:30' }),
    historical_live: meta('evaluation_report.md', 9, 10, { model: 'gemini-3.5-flash-lite', role: 'historical' }),
    live_subset: meta('docs/EVALUATION_Q7_TARGETED.md', 1, 1),
    live_generation: { sql_questions: 8, by_gemini: 8, fallbacks: 0 },
    questions: [
      { id: 1, type: 'text2sql', question: 'How many portfolios do we have in total?', deterministic: result('PASS', 'rule_based'), live: result('PASS'), live_failures: [], historical_live: result('PASS'), historical_live_failures: [], targeted: null },
      { id: 7, type: 'text2sql', question: 'Technology value per portfolio', deterministic: result('PASS', 'rule_based'), live: result('PASS'), live_failures: [], historical_live: result('FAIL'), historical_live_failures: ['Row count mismatch: expected 8, got 13.'], targeted: result('PASS') },
    ],
  };

  it('shows only the deterministic and final live results, never a superseded or targeted run', async () => {
    handler = (url) => (url === '/api/health' ? HEALTH : DATA);   // the API payload still carries the older runs
    renderAt('/evaluation');
    expect(await screen.findByText('Final live Gemini evaluation')).toBeInTheDocument();
    expect(screen.getByText('Deterministic evaluation')).toBeInTheDocument();
    expect(screen.getAllByText('10 / 10')).toHaveLength(2);   // final live + deterministic
    const finalTile = screen.getByText('Final live Gemini evaluation').closest('.card') as HTMLElement;
    expect(within(finalTile).getByText('10 / 10')).toBeInTheDocument();
    expect(screen.getByText(/docs\/EVALUATION_FINAL_LIVE_20261109\.md/)).toBeInTheDocument();
    expect(screen.getByText(/8 of 8 SQL questions by Gemini · 0 fallbacks/)).toBeInTheDocument();
    const q7Row = screen.getByText('Technology value per portfolio', { exact: false }).closest('tr') as HTMLElement;
    expect(within(q7Row).getAllByText('PASS')).toHaveLength(2);          // deterministic + final live
    expect(q7Row.className).not.toMatch(/negative/);
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['#', 'Question', 'Tool routed', 'Deterministic', 'Live Gemini (final)', 'Notes']);
    const text = document.body.textContent ?? '';
    for (const hidden of [/Earlier live run/, /Targeted live re-test/, /Targeted re-test/, /Earlier run/, /Superseded/, /9 \/ 10/, /Q7 PASS/, /evaluation_report\.md/, /EVALUATION_Q7_TARGETED/]) expect(text).not.toMatch(hidden);
    for (const forbidden of [/Run hash/, /95% Aggregate/, /v2\.4/, /100% Match/]) expect(text).not.toMatch(forbidden);
  });

  it('says plainly when a report is missing instead of inventing a score', async () => {
    handler = (url) => (url === '/api/health' ? HEALTH : { ...DATA, live: null, historical_live: null, live_generation: null, live_subset: null, questions: DATA.questions.map((q) => ({ ...q, live: null, historical_live: null, targeted: null })) });
    renderAt('/evaluation');
    expect(await screen.findAllByText('No report on file')).toHaveLength(1);   // the final live tile
    expect(screen.queryByText('No targeted run on file')).not.toBeInTheDocument();
    expect(screen.queryByText('9 / 10')).not.toBeInTheDocument();
  });
});

describe('Exposure', () => {
  it('shows the derived remainder as "Excluded from exposure" and never as bonds', async () => {
    const balanced = { status: 'success', tool: 'exposure_calculator', portfolio_id: 1, portfolio_name: 'Growth Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 4, excluded_bond_holdings: 4, unmapped_equity_holdings: 0, error: null, total_equity_weight_pct: 36, exposures: [{ sector: 'Technology', exposure_pct: 22, weight_fraction: 0.22, holding_count: 2 }, { sector: 'Financials', exposure_pct: 8, weight_fraction: 0.08, holding_count: 1 }, { sector: 'Healthcare', exposure_pct: 6, weight_fraction: 0.06, holding_count: 1 }] };
    handler = (url) => {
      if (url === '/api/health') return HEALTH;
      if (url === '/api/portfolios') return PORTFOLIOS;
      if (url === '/api/exposure') return balanced;
      return detail(PORTFOLIOS[0], false);
    };
    renderAt('/exposure?portfolio=Growth%20Equity%20Fund');
    expect((await screen.findAllByText('Excluded from exposure')).length).toBeGreaterThan(0);
    expect(screen.getByText('64% of weight excluded from exposure')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('img', { name: /Exposure composition/ })).toBeInTheDocument());
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/Excluded \(Bonds\)/);
    expect(text).not.toMatch(/Total Sum: 100/);
  });
});

describe('Exposure verification status', () => {
  const METHOD = 'Independent recomputation in Python from raw rows; no calculator code or SQL aggregation is shared.';
  const base = { tool: 'exposure_calculator', portfolio_id: 1, portfolio_name: 'Growth Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 4, excluded_bond_holdings: 4, unmapped_equity_holdings: 0 };
  const figures = [{ sector: 'Technology', exposure_pct: 22, weight_fraction: 0.22, holding_count: 2 }, { sector: 'Financials', exposure_pct: 8, weight_fraction: 0.08, holding_count: 1 }];
  const block = (status: string, extra: Record<string, unknown> = {}) => ({ status, verified: status === 'verified', method: METHOD, tolerance_pp: 0.0001, checks_run: 20, mismatch_count: status === 'failed' ? 2 : 0, error: null, ...extra });
  const verified = { ...base, status: 'success', error: null, total_equity_weight_pct: 30, exposures: figures, verification: block('verified') };
  const failed = { ...base, status: 'error', exposures: [], total_equity_weight_pct: 0, error: 'The exposure result failed independent verification and was not accepted: Technology: exposure 29% but the data gives 22%.', verification: block('failed') };
  const unverified = { ...base, status: 'error', exposures: [], total_equity_weight_pct: 0, error: 'Independent verification could not be completed, so the exposure figures have been withheld. Verification could not be completed (ReadOnlyQueryError).', verification: block('unverified', { error: 'Verification could not be completed (ReadOnlyQueryError).' }) };

  const openExposure = async (payload: unknown) => {
    handler = (url) => (url === '/api/health' ? HEALTH : url === '/api/portfolios' ? PORTFOLIOS : url === '/api/exposure' ? payload : detail(PORTFOLIOS[0], false));
    renderAt('/exposure?portfolio=Growth%20Equity%20Fund');
  };
  const askExposure = async (payload: unknown) => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : { status: (payload as { status: string }).status, question: 'q', selected_tool: 'exposure_calculator', route_reason: 'Question matched sector-exposure intent.', error: (payload as { error: string | null }).error, result: payload });
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'sector exposure for growth equity');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
  };

  it('Exposure page: a verified result shows "Independently verified" next to the figures', async () => {
    await openExposure(verified);
    expect(await screen.findByText('Independently verified')).toBeInTheDocument();
    expect(screen.getByText('Independently verified').closest('span')).toHaveAttribute('title', expect.stringContaining('Independent recomputation'));
    expect(screen.getByText('Total equity weight')).toBeInTheDocument();
    expect(screen.queryByText('Verification failed')).not.toBeInTheDocument();
    expect(screen.queryByText('Could not be verified')).not.toBeInTheDocument();
  });

  it('Exposure page: a failed verification shows the failure and no exposure figures', async () => {
    await openExposure(failed);
    expect(await screen.findByText('Verification failed')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('failed independent verification');
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /Exposure composition/ })).not.toBeInTheDocument();
  });

  it('Exposure page: an unverified result is visibly different from a failed one and shows no figures', async () => {
    await openExposure(unverified);
    expect(await screen.findByText('Could not be verified')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('figures have been withheld');
    expect(screen.queryByText('Verification failed')).not.toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /Exposure composition/ })).not.toBeInTheDocument();
  });

  it('Ask and Exposure page: with no verifier configured the figures are withheld and the answer says why', async () => {
    const disabled = { ...base, status: 'error', exposures: [], total_equity_weight_pct: 0, error: 'Independent verification could not be completed, so the exposure figures have been withheld. Independent financial verification is disabled or unavailable.', verification: block('unverified', { error: 'Independent financial verification is disabled or unavailable.' }) };
    await openExposure(disabled);
    expect(await screen.findByText('Could not be verified')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('disabled or unavailable');
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Verification failed')).not.toBeInTheDocument();   // distinct from a disproved result
  });

  it('Exposure page: results without a verification block (older servers) show no badge and still render', async () => {
    const { verification: _unused, ...plain } = verified;
    await openExposure(plain);
    expect(await screen.findByText('Total equity weight')).toBeInTheDocument();
    expect(screen.queryByText(/verif/i)).not.toBeInTheDocument();
  });

  it('Ask: a verified exposure answer shows the verification badge beside the figures', async () => {
    await askExposure(verified);
    expect(await screen.findByText('Independently verified')).toBeInTheDocument();
    expect(screen.getByText('Total equity weight')).toBeInTheDocument();
  });

  it('Ask: failed and unverified exposure answers show their own badge, the reason and no figures', async () => {
    await askExposure(failed);
    expect(await screen.findByText('Verification failed')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('failed independent verification');
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
  });

  it('Ask: an unverified exposure answer says the figures were withheld', async () => {
    await askExposure(unverified);
    expect(await screen.findByText('Could not be verified')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('figures have been withheld');
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(screen.queryByText('Verification failed')).not.toBeInTheDocument();
  });
});

describe('Hybrid analysis answers', () => {
  const sqlPart = {
    status: 'success', tool: 'sql_query', original_question: 'q', question: 'Show the holdings in the International Equity Fund',
    generated_sql: 'SELECT s.symbol, h.current_weight FROM holdings h JOIN securities s ON h.security_id = s.security_id WHERE LOWER(h.x) = LOWER(?)', query_parameters: ['international equity fund'],
    row_count: 2, columns: ['symbol', 'current_weight'], rows: [{ symbol: 'CVX', current_weight: 0.2 }, { symbol: 'XOM', current_weight: 0.18 }],
    generation_method: 'rule_based', model_name: null, fallback_used: false, fallback_reason: null, query_description: null, error: null,
  };
  const verification = (status: string) => ({ status, verified: status === 'verified', method: 'Independent recomputation in Python.', tolerance_pp: 0.0001, checks_run: 20, mismatch_count: 0, error: null });
  const exposurePart = {
    status: 'success', tool: 'exposure_calculator', portfolio_id: 7, portfolio_name: 'International Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 8,
    excluded_bond_holdings: 0, unmapped_equity_holdings: 0, error: null, total_equity_weight_pct: 38,
    exposures: [{ sector: 'Energy', exposure_pct: 38, weight_fraction: 0.38, holding_count: 2 }], verification: verification('verified'),
  };
  const withheld = (status: string, error: string) => ({ ...exposurePart, status: 'error', exposures: [], total_equity_weight_pct: 0, error, verification: verification(status) });
  const hybrid = (over: Record<string, unknown>, top: Record<string, unknown> = {}): AgentResponse => {
    const components = (over.components ?? { sql: sqlPart, exposure: exposurePart }) as Record<string, { status: string }>;
    const result = {
      tool: 'hybrid_analysis', status: 'success', complete: true, portfolio: { portfolio_id: 7, portfolio_name: 'International Equity Fund' },
      component_status: { sql: components.sql?.status ?? 'not_run', exposure: components.exposure?.status ?? 'not_run' }, components, issues: [], summary: 'Both parts completed.', error: null, ...over,
    };
    return { status: result.status as AgentResponse['status'], question: 'q', selected_tool: 'hybrid_analysis', route_reason: 'Question asks for both portfolio data and sector exposure.', error: result.error as string | null, result: result as never, ...top };
  };
  const ask = async (payload: AgentResponse) => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : payload);
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'holdings and sector exposure');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
  };

  it('a complete hybrid answer shows the SQL table, the verified exposure and no partial notice', async () => {
    await ask(hybrid({}));
    const sqlSection = await screen.findByRole('region', { name: 'Holdings (SQL component)' });
    expect(within(sqlSection).getByRole('table')).toBeInTheDocument();
    expect(within(sqlSection).getByText('CVX')).toBeInTheDocument();
    expect(within(sqlSection).getByRole('region', { name: 'Generated SQL, formatted' })).toBeInTheDocument();
    const expSection = screen.getByRole('region', { name: 'Sector exposure (deterministic component)' });
    expect(within(expSection).getByText('Independently verified')).toBeInTheDocument();
    expect(within(expSection).getByText('Total equity weight')).toBeInTheDocument();
    expect(screen.getByText('Hybrid · SQL + Exposure')).toBeInTheDocument();
    expect(screen.queryByText(/Partial result/)).not.toBeInTheDocument();
    const details = screen.getByRole('region', { name: 'Query details' });
    expect(within(details).getByText('Hybrid analysis (SQL + Exposure)')).toBeInTheDocument();
    expect(within(details).getByText('SQL component')).toBeInTheDocument();
  });

  it('SQL failure with a verified exposure is shown as a partial result and never as complete', async () => {
    const failedSql = { ...sqlPart, status: 'error', rows: [], columns: [], row_count: 0, generated_sql: null, error: 'SQL generation failed (simulated)' };
    await ask(hybrid({ status: 'partial', complete: false, components: { sql: failedSql, exposure: exposurePart }, issues: ['SQL component error: SQL generation failed (simulated)'], summary: 'Partial result: only part of the request could be completed. SQL component error: SQL generation failed (simulated)', error: 'SQL component error: SQL generation failed (simulated)' }));
    expect(await screen.findByText('Partial result: not everything you asked for could be completed')).toBeInTheDocument();
    expect(screen.getByText('PARTIAL')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Holdings (SQL component)' })).getByRole('alert')).toHaveTextContent('could not be completed: SQL generation failed (simulated)');
    expect(within(screen.getByRole('region', { name: 'Holdings (SQL component)' })).queryByRole('table')).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Sector exposure (deterministic component)' })).getByText('Independently verified')).toBeInTheDocument();
  });

  it('exposure failure with a successful SQL part keeps the table and withholds exposure figures', async () => {
    await ask(hybrid({ status: 'partial', complete: false, components: { sql: sqlPart, exposure: withheld('failed', 'The exposure result failed independent verification and was not accepted: Energy: exposure 45% but the data gives 38%.') }, issues: ['Exposure component failed independent verification; its figures were withheld.'], summary: 'Partial result: only part of the request could be completed. Exposure component failed independent verification; its figures were withheld.', error: 'Exposure component failed independent verification; its figures were withheld.' }));
    const expSection = await screen.findByRole('region', { name: 'Sector exposure (deterministic component)' });
    expect(within(expSection).getByText('Verification failed')).toBeInTheDocument();
    expect(within(expSection).getByRole('alert')).toHaveTextContent('failed independent verification');
    expect(within(expSection).queryByText('Total equity weight')).not.toBeInTheDocument();
    expect(within(expSection).queryByRole('img', { name: /Exposure composition/ })).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Holdings (SQL component)' })).getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('Partial result: not everything you asked for could be completed')).toBeInTheDocument();
  });

  it('an unverified exposure is shown as such (distinct from failed) with the figures withheld', async () => {
    await ask(hybrid({ status: 'partial', complete: false, components: { sql: sqlPart, exposure: withheld('unverified', 'Independent verification could not be completed, so the exposure figures have been withheld.') }, issues: ['Exposure component could not be independently verified; its figures were withheld.'], summary: 'Partial result.', error: 'x' }));
    const expSection = await screen.findByRole('region', { name: 'Sector exposure (deterministic component)' });
    expect(within(expSection).getByText('Could not be verified')).toBeInTheDocument();
    expect(within(expSection).queryByText('Verification failed')).not.toBeInTheDocument();
    expect(within(expSection).queryByText('Total equity weight')).not.toBeInTheDocument();
  });

  it('when neither part completes it says so and shows no figures or tables', async () => {
    const failedSql = { ...sqlPart, status: 'error', rows: [], columns: [], row_count: 0, error: 'boom' };
    await ask(hybrid({ status: 'error', complete: false, components: { sql: failedSql, exposure: withheld('unverified', 'Independent verification could not be completed, so the exposure figures have been withheld.') }, issues: ['a', 'b'], summary: 'Neither part of the request could be completed.', error: 'a b' }));
    expect(await screen.findByText('Neither part could be completed')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
  });

  it('an unresolved portfolio is an ordinary error notice', async () => {
    await ask({ status: 'error', question: 'q', selected_tool: 'hybrid_analysis', route_reason: 'r', error: 'Could not determine which portfolio to analyse.', result: null });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not determine which portfolio');
  });

  it('simple SQL and exposure answers still render exactly as before', async () => {
    await ask(sqlResponse());
    expect(await screen.findByText('SUCCESS')).toBeInTheDocument();
    expect(screen.queryByText('Hybrid · SQL + Exposure')).not.toBeInTheDocument();
  });
});

describe('Final-answer verification status', () => {
  const av = (status: string, over: Record<string, unknown> = {}) => ({
    status, verified: status === 'verified', method: 'Structural consistency checks between the response and the tool results.', scope: 'Model-written text cannot be verified.',
    checks_run: 12, mismatch_count: status === 'failed' ? 1 : 0, mismatches: status === 'failed' ? [{ code: 'row_count_mismatch', message: 'row_count is 3 but 2 rows were returned.', component: 'sql' }] : [],
    checks_skipped: [], unverified_claims: [], summary: '2 rows returned (2 columns).', error: status === 'unverified' ? 'Answer verification could not be completed (RuntimeError).' : null, ...over,
  });
  const ask = async (payload: AgentResponse) => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : payload);
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'top holdings');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
  };

  it('a verified SQL answer shows "Answer verified", a deterministic headline and labels the model sentence as unverified', async () => {
    await ask(sqlResponse({ generation_method: 'gemini', model_name: 'gemini-test', query_description: 'Top holdings by cost basis', answer_verification: av('verified', { unverified_claims: ['query_description'] }) } as never));
    expect(await screen.findByText('Answer verified')).toBeInTheDocument();
    expect(screen.getByText('2 rows returned (2 columns).')).toBeInTheDocument();
    expect(screen.getByText('Model-generated description (not verified):')).toBeInTheDocument();
    expect(screen.getAllByText(/Top holdings by cost basis/).length).toBeGreaterThan(0);
    expect(screen.getByText('Answer verified').closest('span')).toHaveAttribute('title', expect.stringContaining('Model-written text is not verified'));
  });

  it('without a model sentence there is no description line and no unverified-text label', async () => {
    await ask(sqlResponse({ answer_verification: av('verified') } as never));
    expect(await screen.findByText('Answer verified')).toBeInTheDocument();
    expect(screen.queryByText('Model-generated description (not verified):')).not.toBeInTheDocument();
  });

  it('a failed SQL answer shows the failure and no table or SQL', async () => {
    const failed = sqlResponse({ status: 'error', rows: [], columns: [], row_count: 0, generated_sql: null, error: 'The SQL result failed answer verification and was withheld: row_count is 3 but 2 rows were returned.', answer_verification: av('failed') } as never, { status: 'error', error: 'The SQL result failed answer verification and was withheld: row_count is 3 but 2 rows were returned.' });
    await ask(failed);
    expect(await screen.findByText('Answer verification failed')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('withheld');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('Answer verified')).not.toBeInTheDocument();
  });

  it('an unverifiable answer is distinct from a failed one', async () => {
    await ask(sqlResponse({ status: 'error', rows: [], columns: [], row_count: 0, generated_sql: null, error: 'withheld', answer_verification: av('unverified') } as never, { status: 'error', error: 'Answer verification could not be completed, so the result was withheld.' }));
    expect(await screen.findByText('Answer could not be verified')).toBeInTheDocument();
    expect(screen.queryByText('Answer verification failed')).not.toBeInTheDocument();
    expect(screen.queryByText('Answer verified')).not.toBeInTheDocument();
  });

  it('figures that changed after financial verification are reported as a failed answer, not as "Independently verified"', async () => {
    const result = { tool: 'exposure_calculator', status: 'error', portfolio_id: 7, portfolio_name: 'International Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 8, excluded_bond_holdings: 0, unmapped_equity_holdings: 0, exposures: [], total_equity_weight_pct: 0, error: 'The exposure result failed answer verification and was withheld.', verification: { status: 'verified', verified: true, method: 'm', tolerance_pp: 0.0001, checks_run: 10, mismatch_count: 0, error: null }, answer_verification: av('failed', { summary: null }) };
    await ask({ status: 'error', question: 'q', selected_tool: 'exposure_calculator', route_reason: 'r', error: 'The exposure result failed answer verification and was withheld.', result } as never);
    expect(await screen.findByText('Answer verification failed')).toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Total equity weight')).not.toBeInTheDocument();
  });

  const hybridPayload = (status: 'success' | 'partial', answer: Record<string, unknown>): AgentResponse => ({
    status, question: 'q', selected_tool: 'hybrid_analysis', route_reason: 'r', error: status === 'success' ? null : 'SQL component error: boom',
    result: {
      tool: 'hybrid_analysis', status, complete: status === 'success', portfolio: { portfolio_id: 7, portfolio_name: 'International Equity Fund' },
      component_status: { sql: status === 'success' ? 'success' : 'error', exposure: 'success' }, issues: status === 'success' ? [] : ['SQL component error: boom'], summary: status === 'success' ? 'Both parts completed.' : 'Partial result: SQL component error: boom',
      error: status === 'success' ? null : 'SQL component error: boom', answer_verification: answer,
      components: {
        sql: { status: status === 'success' ? 'success' : 'error', tool: 'sql_query', original_question: 'q', question: 'Show the holdings in the International Equity Fund', generated_sql: status === 'success' ? 'SELECT 1' : null, query_parameters: [], row_count: status === 'success' ? 1 : 0, columns: status === 'success' ? ['symbol'] : [], rows: status === 'success' ? [{ symbol: 'CVX' }] : [], generation_method: 'rule_based', model_name: null, fallback_used: false, fallback_reason: null, query_description: null, error: status === 'success' ? null : 'boom' },
        exposure: { status: 'success', tool: 'exposure_calculator', portfolio_id: 7, portfolio_name: 'International Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 8, excluded_bond_holdings: 0, unmapped_equity_holdings: 0, error: null, total_equity_weight_pct: 38, exposures: [{ sector: 'Energy', exposure_pct: 38, weight_fraction: 0.38, holding_count: 2 }], verification: { status: 'verified', verified: true, method: 'm', tolerance_pp: 0.0001, checks_run: 10, mismatch_count: 0, error: null } },
      },
    } as never,
  });

  it('a complete hybrid answer is "Answer verified"; a partial one is labelled partial and never styled as complete', async () => {
    await ask(hybridPayload('success', av('verified', { summary: 'Both parts completed.' })));
    expect(await screen.findByText('Answer verified')).toBeInTheDocument();
    expect(screen.queryByText('PARTIAL')).not.toBeInTheDocument();
  });

  it('partial hybrid: shows PARTIAL and the partial label instead of "Answer verified"', async () => {
    await ask(hybridPayload('partial', av('verified', { summary: 'Partial result: SQL component error: boom' })));
    expect(await screen.findByText('Partial · shown parts consistent')).toBeInTheDocument();
    expect(screen.getByText('PARTIAL')).toBeInTheDocument();
    expect(screen.queryByText('Answer verified')).not.toBeInTheDocument();
    expect(screen.getByText('Partial result: not everything you asked for could be completed')).toBeInTheDocument();
  });
});

describe('Scenario analysis', () => {
  const DISCLAIMER = 'Hypothetical scenario for illustration only. It is not a forecast or prediction and not actual portfolio performance.';
  const ver = (status: string) => ({ status, verified: status === 'verified', method: 'Independent recomputation in Python (exact fractions).', tolerance_pp: 0.0001, checks_run: 40, mismatch_count: status === 'failed' ? 1 : 0, error: null });
  const scenarioOk = {
    tool: 'scenario_analysis', status: 'success', portfolio_id: 7, portfolio_name: 'International Equity Fund',
    scenario: { id: 'technology_sell_off', name: 'Technology Sell-off', description: 'Illustrative: Technology falls 25%; all other sectors are unchanged.', type: 'predefined', market_shock_pct: 0, sector_shocks: { Technology: -25 }, is_hypothetical: true, is_forecast: false },
    starting_exposure: [], unheld_sector_shocks: [{ sector: 'Utilities', shock_pct: -50 }],
    sector_impacts: [
      { sector: 'Energy', exposure_pct: 38, shock_pct: 0, contribution_pp: 0, shocked: false },
      { sector: 'Technology', exposure_pct: 36, shock_pct: -25, contribution_pp: -9, shocked: true },
    ],
    estimated_portfolio_impact_pct: -9, estimated_value_impact: { amount: -2520000, currency: 'USD', aum: 28000000, basis: 'total_aum x estimated_portfolio_impact_pct / 100' }, value_impact_note: null,
    coverage: { equity_exposure_pct: 74, shocked_weight_pct: 36, unshocked_mapped_weight_pct: 38, equity_holdings_count: 6, unmapped_equity_holdings: 1, unmapped_equity_weight_pct: 12, excluded_bond_holdings: 2, excluded_bond_weight_pct: 14, recorded_weight_accounted_pct: 100, recorded_weight_unaccounted_pct: 0, renormalised: false },
    assumptions: ['Weights are used as recorded: they are not renormalised and are not forced to total 100%.'],
    disclaimer: DISCLAIMER, error: null, verification: ver('verified'),
  };
  const withheld = (status: string, message: string) => ({ ...scenarioOk, status: 'error', sector_impacts: [], starting_exposure: [], unheld_sector_shocks: [], estimated_portfolio_impact_pct: null, estimated_value_impact: null, coverage: null, error: message, verification: ver(status) });
  const wrap = (result: unknown, status = (result as { status: string }).status): AgentResponse => ({ status: status as AgentResponse['status'], question: 'q', selected_tool: 'scenario_analysis', route_reason: 'Question asks for a hypothetical scenario or stress test; routed to the deterministic scenario tool (no Gemini).', error: (result as { error: string | null }).error, result: result as never });
  const ask = async (payload: AgentResponse) => {
    const user = userEvent.setup();
    handler = (url) => (url === '/api/health' ? HEALTH : payload);
    renderAt('/ask');
    await user.type(screen.getByLabelText('Your question'), 'stress test the fund');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
  };

  it('Ask: a verified scenario shows assumptions, the sector table, totals, coverage, verification and the disclaimer', async () => {
    await ask(wrap(scenarioOk));
    const panel = await screen.findByRole('region', { name: 'Scenario analysis (hypothetical)' });
    expect(within(panel).getByText('Technology Sell-off')).toBeInTheDocument();
    expect(within(panel).getByText('Hypothetical')).toBeInTheDocument();
    expect(within(panel).getByText('Independently verified')).toBeInTheDocument();
    expect(within(panel).getByText('Illustrative: Technology falls 25%; all other sectors are unchanged.')).toBeInTheDocument();
    const table = within(panel).getByRole('table');
    expect(within(table).getByText('Technology')).toBeInTheDocument();
    expect(within(table).getByText('-25.00%')).toBeInTheDocument();
    expect(within(table).getAllByText('-9.00 pp').length).toBe(2);       // the row contribution and the total
    expect(within(panel).getByLabelText('Estimated portfolio impact')).toHaveTextContent('-9.00 pp');
    expect(within(panel).getByText('−$2,520,000')).toBeInTheDocument();
    expect(within(panel).getByText(/Unmapped stock holdings \(1\)/)).toBeInTheDocument();
    expect(within(panel).getByText(/Bonds excluded \(2\)/)).toBeInTheDocument();
    expect(within(panel).getByText(/Estimated total impact \(not renormalised\)/)).toBeInTheDocument();
    expect(within(panel).getByRole('note')).toHaveTextContent(DISCLAIMER);
    expect(within(panel).getByText(/Utilities -50.00%: no effect on this portfolio/)).toBeInTheDocument();
    expect(screen.getByText('Scenario · hypothetical')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Query details' })).getByText('Scenario analysis (deterministic, hypothetical)')).toBeInTheDocument();
    expect(document.body.textContent ?? '').not.toMatch(/\bforecast\b[^.]*\bwill\b|prediction:/i);
  });

  it('Ask: failed and unverified scenarios show their own badge, the reason, no figures and still the disclaimer', async () => {
    await ask(wrap(withheld('failed', 'The scenario result failed independent verification and was not accepted: contribution mismatch.')));
    expect(await screen.findByText('Verification failed')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('failed independent verification');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent(DISCLAIMER);
  });

  it('Ask: an unverified scenario is distinct from a failed one and shows no figures', async () => {
    await ask(wrap(withheld('unverified', 'Independent verification could not be completed, so the scenario figures have been withheld.')));
    expect(await screen.findByText('Could not be verified')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('figures have been withheld');
    expect(screen.queryByText('Verification failed')).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Estimated portfolio impact')).not.toBeInTheDocument();
  });

  const answerCheck = (status: string, over: Record<string, unknown> = {}) => ({
    status, verified: status === 'verified', method: 'Structural consistency checks between the response and the tool results.', scope: 'Model-written text cannot be verified.',
    checks_run: 30, mismatch_count: status === 'failed' ? 1 : 0, mismatches: status === 'failed' ? [{ code: 'figures_changed_after_verification', message: 'The scenario figures about to be displayed are not the figures that were verified.', component: 'scenario' }] : [],
    checks_skipped: [], unverified_claims: [], summary: 'Hypothetical scenario “Technology Sell-off” for International Equity Fund: 2 sectors, bonds excluded, weights not renormalised.', error: null, ...over,
  });

  it('Ask: a scenario whose answer check passed shows "Answer verified" beside the independent verification', async () => {
    await ask(wrap({ ...scenarioOk, answer_verification: answerCheck('verified') }));
    expect(await screen.findByText('Answer verified')).toBeInTheDocument();
    expect(screen.getByText('Independently verified')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Query details' })).getByText('Answer verification')).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it('Ask: a scenario that failed the answer check shows that failure, never "Independently verified", and no figures', async () => {
    const result = { ...withheld('verified', 'The scenario result failed answer verification and was withheld: The scenario figures about to be displayed are not the figures that were verified.'), answer_verification: answerCheck('failed') };
    await ask(wrap(result));
    expect(await screen.findByText('Answer verification failed')).toBeInTheDocument();
    expect(screen.queryByText('Independently verified')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('failed answer verification');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent(DISCLAIMER);
  });

  it('Ask: invalid input is an ordinary error with no table, no verification badge', async () => {
    await ask(wrap({ tool: 'scenario_analysis', status: 'error', error_type: 'invalid_scenario', portfolio_id: null, portfolio_name: null, scenario: null, starting_exposure: [], sector_impacts: [], unheld_sector_shocks: [], estimated_portfolio_impact_pct: null, estimated_value_impact: null, coverage: null, assumptions: [], disclaimer: DISCLAIMER, error: 'market shock must be between -100% and +100%, got 250%.' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('between -100%');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    for (const badge of ['Independently verified', 'Verification failed', 'Could not be verified']) expect(screen.queryByText(badge)).not.toBeInTheDocument();
  });

  it('Ask: a bond-only portfolio explains there is nothing to shock; a missing AUM shows the note instead of a currency amount', async () => {
    await ask(wrap({ ...scenarioOk, status: 'no_equity_holdings', sector_impacts: [], estimated_portfolio_impact_pct: 0, estimated_value_impact: null, value_impact_note: 'No positive total AUM is recorded for this portfolio, so no currency impact is shown.', unheld_sector_shocks: [] }));
    expect(await screen.findByText('No equity holdings')).toBeInTheDocument();
    expect(screen.getByText(/No positive total AUM is recorded/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  const options = { scenarios: [{ id: 'broad_market_downturn', name: 'Broad Market Downturn', description: 'Illustrative: every mapped stock sector falls 10%.', market_shock_pct: -10, sector_shocks: {} }, { id: 'energy_shock', name: 'Energy Shock', description: 'Illustrative: Energy falls 20%.', market_shock_pct: 0, sector_shocks: { Energy: -20 } }], sectors: ['Energy', 'Technology'], shock_limits_pct: { min: -100, max: 100 } };
  const exposureOk = { status: 'success', tool: 'exposure_calculator', portfolio_id: 7, portfolio_name: 'International Equity Fund', exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 8, excluded_bond_holdings: 0, unmapped_equity_holdings: 0, error: null, total_equity_weight_pct: 38, exposures: [{ sector: 'Energy', exposure_pct: 38, weight_fraction: 0.38, holding_count: 2 }], verification: ver('verified') };
  const openWorkspace = (onScenario: (body: Record<string, unknown>) => unknown) => {
    handler = (url, init) => {
      if (url === '/api/health') return HEALTH;
      if (url === '/api/portfolios') return PORTFOLIOS;
      if (url === '/api/exposure') return exposureOk;
      if (url === '/api/scenarios') return options;
      if (url === '/api/scenario') return onScenario(JSON.parse(String(init?.body)));
      return detail(PORTFOLIOS[0], false);
    };
    renderAt('/exposure?portfolio=Growth%20Equity%20Fund');
  };

  it('Exposure workspace: runs a predefined scenario for the selected portfolio and shows the hypothetical result', async () => {
    const user = userEvent.setup();
    let sent: Record<string, unknown> | null = null;
    openWorkspace((body) => { sent = body; return scenarioOk; });
    const section = await screen.findByRole('region', { name: 'Scenario analysis' });
    expect(within(section).getByText(/hypothetical, not forecasts and not actual performance/)).toBeInTheDocument();
    await within(section).findByRole('option', { name: 'Energy Shock' });
    await user.selectOptions(within(section).getByLabelText('Scenario'), 'energy_shock');
    expect(within(section).getByText('Illustrative: Energy falls 20%.')).toBeInTheDocument();
    await user.click(within(section).getByRole('button', { name: 'Run scenario' }));
    expect(await within(section).findByRole('region', { name: 'Scenario analysis (hypothetical)' })).toBeInTheDocument();
    expect(sent).toEqual({ portfolio_name: 'International Equity Fund', scenario_id: 'energy_shock' });
    expect(within(section).getByLabelText('Estimated portfolio impact')).toHaveTextContent('-9.00 pp');
  });

  it('Exposure workspace: custom shocks are sent as numbers and the button needs at least one shock', async () => {
    const user = userEvent.setup();
    let sent: Record<string, unknown> | null = null;
    openWorkspace((body) => { sent = body; return scenarioOk; });
    const section = await screen.findByRole('region', { name: 'Scenario analysis' });
    await within(section).findByRole('option', { name: 'Custom shocks…' });
    await user.selectOptions(within(section).getByLabelText('Scenario'), 'custom');
    expect(within(section).getByRole('button', { name: 'Run scenario' })).toBeDisabled();
    await user.type(within(section).getByLabelText('Market-wide shock (%)'), '-15');
    await within(section).findByRole('option', { name: 'Technology' });
    await user.selectOptions(within(section).getByLabelText('Sector'), 'Technology');
    await user.type(within(section).getByLabelText('Sector shock (%)'), '-25');
    await user.click(within(section).getByRole('button', { name: 'Run scenario' }));
    await within(section).findByRole('region', { name: 'Scenario analysis (hypothetical)' });
    expect(sent).toEqual({ portfolio_name: 'International Equity Fund', market_shock_pct: -15, sector_shocks: { Technology: -25 } });
  });

  it('Exposure workspace: a withheld scenario shows the notice and no figures', async () => {
    const user = userEvent.setup();
    openWorkspace(() => withheld('unverified', 'Independent verification could not be completed, so the scenario figures have been withheld.'));
    const section = await screen.findByRole('region', { name: 'Scenario analysis' });
    await user.click(within(section).getByRole('button', { name: 'Run scenario' }));
    expect(await within(section).findByText('Could not be verified')).toBeInTheDocument();
    expect(within(section).queryByRole('table')).not.toBeInTheDocument();
  });
});

describe('About', () => {
  it('explains the pipeline, safeguards and limitations', () => {
    renderAt('/about');
    expect(screen.getByRole('heading', { name: 'How the agent answers your question' })).toBeInTheDocument();
    expect(screen.getByText('Read-only by design')).toBeInTheDocument();
    expect(screen.getByText('Known limitations')).toBeInTheDocument();
  });

  it('expands FAQ answers', async () => {
    const user = userEvent.setup();
    renderAt('/about');
    await user.click(screen.getByRole('button', { name: 'Can this place trades?' }));
    expect(screen.getByText(/no broker, exchange or order connectivity/)).toBeInTheDocument();
  });
});
