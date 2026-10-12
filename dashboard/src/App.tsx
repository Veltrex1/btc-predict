import { useEffect, useState } from 'react';
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

// ─── API types (see server/app.py) ──────────────────────────────────
interface MonthStats {
  period: string; windows_eligible: number; calls: number; settled_calls: number; correct: number;
  accuracy: number | null; acc_ci_low: number | null; acc_ci_high: number | null; coverage: number | null;
  pnl_1_contract: number | null;
}
interface Signal {
  status: string; state?: 'WATCHING' | 'LOCKED' | 'NO_CALL'; side?: 'ABOVE' | 'BELOW';
  prob_above?: number; locked_prob?: number | null; locked_secs_left?: number | null;
  window_open?: string; window_close?: string; secs_left?: number; price?: number; target?: number;
  target_source?: string; distance_pct?: number; ticker?: string | null; yes_bid?: number | null;
  yes_ask?: number | null; no_ask?: number | null; edge_above?: number | null; edge_below?: number | null;
  model?: string; tau?: number; min_lead_s?: number; month?: MonthStats;
}
interface Bar { ts: string; open: number; high: number; low: number; close: number }
interface Snapshot { ts: string; secs_left: number; price: number; prob: number }
interface WindowRow {
  window_open: string; target: number; locked_side: string | null; locked_prob: number | null;
  locked_secs_left: number | null; result: string | null; yes_ask_at_lock: number | null; no_ask_at_lock: number | null;
}

interface Trade { ts: string; kind: string; side: string; count: number; price: number; fee: number; reason: string; ticker: string; cash_after: number }
interface Portfolio {
  mode: 'off' | 'shadow' | 'live'; allocation?: number; equity?: number; cash?: number; pnl?: number; stop_at?: number;
  halted?: string | null; trade_until?: string; held?: { ticker: string; side: string; count: number; price: number } | null;
  account?: { cash: number; portfolio_value: number; total: number } | null; last_error?: string | null;
  trades?: Trade[]; trade_count?: number;
}

interface LoopRow { candidate: string; windows?: number; pnl_recent?: number | null; pnl_prior?: number | null; t_stat?: number; promote?: boolean; error?: string }
interface Research {
  loop: { ts: string; results: LoopRow[] } | null;
  recorder: Record<string, number | string | null>;
  bot: { mode: string; equity: number; pnl: number; trade_count: number; halted: string | null; allocation: number } | null;
}

// ─── helpers ─────────────────────────────────────────────────────────
const usd = (n?: number | null) => n == null ? '–' : n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const pct = (n?: number | null, d = 1) => n == null ? '–' : `${(n * 100).toFixed(d)}%`;
const cents = (n?: number | null) => n == null ? '–' : `${Math.round(n * 100)}¢`;
const clock = (s?: number | null) => s == null ? '–' : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const et = (iso?: string) => iso ? new Date(iso + 'Z').toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false }) : '–';

// When the server is internet-facing it needs the API token: open the dashboard once with ?token=..., it is remembered.
const TOKEN = (() => {
  const t = new URLSearchParams(window.location.search).get('token');
  try { if (t) localStorage.setItem('btc-token', t); return t ?? localStorage.getItem('btc-token') ?? ''; } catch { return t ?? ''; }
})();
const authed = (url: string, init: RequestInit = {}) =>
  fetch(url, { ...init, headers: { ...(init.headers ?? {}), ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}) } });

function usePoll<T>(url: string, ms: number): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => authed(url).then(r => r.json()).then(d => { if (alive) setData(d); }).catch(() => {});
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
  }, [url, ms]);
  return data;
}

function Panel({ title, children, className = '', grid = '' }: { title?: string; children: React.ReactNode; className?: string; grid?: string }) {
  return (
    <section className={`rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 ${className}`}>
      {title && <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-[var(--ink-3)]">{title}</h2>}
      {grid ? <div className={grid}>{children}</div> : children}
    </section>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="text-xs text-[var(--ink-3)]">{label}</div>
      <div className="num font-mono text-lg">{value}</div>
      {sub && <div className="num text-xs text-[var(--ink-2)]">{sub}</div>}
    </div>
  );
}

// ─── candlestick chart (from the original widget) with target line ─
function Candles({ bars, target, price }: { bars: Bar[]; target?: number; price?: number }) {
  if (!bars.length) return <div className="h-64 text-sm text-[var(--ink-3)]">waiting for bars…</div>;
  const W = 800, H = 260, m = { t: 10, r: 64, b: 24, l: 8 };
  const hi = Math.max(...bars.map(b => b.high), target ?? -Infinity, price ?? -Infinity);
  const lo = Math.min(...bars.map(b => b.low), target ?? Infinity, price ?? Infinity);
  const pad = (hi - lo || 1) * 0.06, yMax = hi + pad, yMin = lo - pad;
  const w = W - m.l - m.r, h = H - m.t - m.b, gap = w / bars.length, bw = gap * 0.6;
  const y = (p: number) => m.t + h - ((p - yMin) / (yMax - yMin)) * h;
  const x = (i: number) => m.l + i * gap + gap / 2;
  const ticks = [0, 1, 2, 3, 4].map(i => yMin + (yMax - yMin) * (i / 4));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-64 w-full" preserveAspectRatio="none" role="img"
         aria-label="BTC one-minute candles for the last hour with the window target">
      {ticks.map((v, i) => (
        <g key={i}>
          <line x1={m.l} x2={W - m.r} y1={y(v)} y2={y(v)} stroke="#1a1a1a" />
          <text x={W - m.r + 6} y={y(v) + 4} fill="#737373" fontSize={10}>{`$${v.toLocaleString('en-US', { maximumFractionDigits: 0 })}`}</text>
        </g>
      ))}
      {bars.map((b, i) => {
        const up = b.close >= b.open, c = up ? 'var(--above)' : 'var(--below)';
        return (
          <g key={b.ts}>
            <title>{`${et(b.ts.replace(' ', 'T'))} ET  O ${b.open.toFixed(2)}  H ${b.high.toFixed(2)}  L ${b.low.toFixed(2)}  C ${b.close.toFixed(2)}`}</title>
            <rect x={x(i) - gap / 2} y={m.t} width={gap} height={h} fill="transparent" />
            <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} stroke={c} />
            <rect x={x(i) - bw / 2} y={Math.min(y(b.open), y(b.close))} width={bw}
                  height={Math.max(Math.abs(y(b.close) - y(b.open)), 1)} fill={c} rx={1} />
          </g>
        );
      })}
      {target != null && (
        <g>
          <line x1={m.l} x2={W - m.r} y1={y(target)} y2={y(target)} stroke="var(--target)" strokeWidth={1.5} strokeDasharray="4 4" />
          <text x={m.l + 4} y={y(target) - 5} fill="var(--target)" fontSize={10}>target {target.toFixed(2)}</text>
        </g>
      )}
      {['00', '15', '30', '45'].flatMap(q => bars.map((b, i) => (b.ts.slice(14, 16) === q ? (
        <text key={b.ts + q} x={x(i)} y={H - 6} fill="#737373" fontSize={10} textAnchor="middle">{et(b.ts.replace(' ', 'T'))}</text>
      ) : null)))}
    </svg>
  );
}

function ProbChart({ snaps, tau, lockedSecsLeft }: { snaps: Snapshot[]; tau: number; lockedSecsLeft?: number | null }) {
  const data = snaps.map(s => ({ t: +(15 - s.secs_left / 60).toFixed(2), p: s.prob }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 20, right: 16, bottom: 4, left: -16 }}>
        <CartesianGrid stroke="#1a1a1a" vertical={false} />
        <XAxis dataKey="t" type="number" domain={[0, 15]} ticks={[0, 3, 6, 9, 12, 15]} stroke="#737373" fontSize={11}
               tickFormatter={v => `${v}m`} />
        <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} stroke="#737373" fontSize={11} tickFormatter={v => `${v * 100}%`} />
        <ReferenceLine y={tau} stroke="var(--above)" strokeDasharray="3 3" label={{ value: `ABOVE ≥ ${pct(tau, 0)}`, fill: '#a3a3a3', fontSize: 10, position: 'insideTopLeft' }} />
        <ReferenceLine y={1 - tau} stroke="var(--below)" strokeDasharray="3 3" label={{ value: `BELOW ≤ ${pct(1 - tau, 0)}`, fill: '#a3a3a3', fontSize: 10, position: 'insideBottomLeft' }} />
        {lockedSecsLeft != null && <ReferenceLine x={+(15 - lockedSecsLeft / 60).toFixed(2)} stroke="#fafafa" label={{ value: 'lock', fill: '#fafafa', fontSize: 10, position: 'top' }} />}
        <Tooltip contentStyle={{ background: '#111', border: '1px solid #262626', fontSize: 12 }}
                 formatter={(v: number) => [pct(v), 'P(above)']} labelFormatter={v => `minute ${v}`} />
        <Line dataKey="p" stroke="var(--prob)" strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

function PortfolioPanel({ pf }: { pf: Portfolio }) {
  const [busy, setBusy] = useState(false);
  const stop = () => { setBusy(true); authed('/trading/stop', { method: 'POST' }).finally(() => setBusy(false)); };
  const pnl = pf.pnl ?? 0;
  return (
    <Panel title={`Bot · ${pf.mode === 'live' ? 'LIVE (real money)' : 'shadow (no real orders)'}`}>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Bot equity" value={usd(pf.equity)} sub={`of ${usd(pf.allocation)} allocated`} />
        <div>
          <div className="text-xs text-[var(--ink-3)]">P&amp;L</div>
          <div className="num font-mono text-lg" style={{ color: pnl >= 0 ? 'var(--above)' : 'var(--below)' }}>{pnl >= 0 ? '+' : ''}{usd(pnl)}</div>
          <div className="num text-xs text-[var(--ink-2)]">stops at {usd(pf.stop_at)}</div>
        </div>
        <Stat label="Kalshi account (live)" value={usd(pf.account?.total)} sub={pf.account ? `cash ${usd(pf.account.cash)}` : 'no API key'} />
        <Stat label="Holding" value={pf.held ? `${pf.held.count.toFixed(2)} ${pf.held.side.toUpperCase()}` : 'flat'} sub={pf.held ? `@ ${(pf.held.price * 100).toFixed(1)}¢` : undefined} />
        <div>
          <div className="text-xs text-[var(--ink-3)]">Status</div>
          <div className="text-sm" style={{ color: pf.halted ? 'var(--below)' : 'var(--ink)' }}>{pf.halted ? `Halted: ${pf.halted}` : `Trading until ${pf.trade_until?.slice(11, 16)} UTC`}</div>
          {!pf.halted && <button onClick={stop} disabled={busy} className="mt-1 rounded-md border border-[var(--below)] px-3 py-1 text-sm text-[var(--below)] hover:bg-[var(--below)] hover:text-black">Stop trading</button>}
        </div>
      </div>
      {pf.last_error && <div className="mt-2 text-xs text-[var(--below)]">{pf.last_error}</div>}
      <div className="mt-4 overflow-x-auto">
        <table className="num w-full text-left font-mono text-sm">
          <thead className="text-xs text-[var(--ink-3)]"><tr><th className="py-1 pr-4">time ET</th><th className="pr-4">action</th><th className="pr-4">contracts</th><th className="pr-4">price</th><th className="pr-4">fee</th><th className="pr-4">why</th><th>bot cash</th></tr></thead>
          <tbody>
            {(pf.trades ?? []).map((t, i) => (
              <tr key={i} className="border-t border-[var(--line)]">
                <td className="py-1 pr-4">{et(t.ts.slice(0, 19))}</td>
                <td className="pr-4" style={{ color: t.kind === 'buy' ? 'var(--ink)' : t.kind === 'sell' ? 'var(--target)' : 'var(--ink-2)' }}>{t.kind} {t.side.toUpperCase()}</td>
                <td className="pr-4">{t.count.toFixed(2)}</td>
                <td className="pr-4">{(t.price * 100).toFixed(1)}¢</td>
                <td className="pr-4">{(t.fee * 100).toFixed(2)}¢</td>
                <td className="pr-4">{t.reason}</td>
                <td>{usd(t.cash_after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function ResearchPanel({ r }: { r: Research }) {
  const rec = r.recorder ?? {};
  const n = (k: string) => (typeof rec[k] === 'number' ? (rec[k] as number).toLocaleString('en-US') : '–');
  const c = (v?: number | null) => (v == null ? '–' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}¢`);
  return (
    <Panel title={`Research loop${r.loop ? ` · last run ${et(r.loop.ts.slice(0, 19))} ET` : ' · not run yet'}`}>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-7">
        <Stat label="Windows recorded" value={n('windows_settled')} sub={rec.since ? `since ${String(rec.since).slice(0, 10)}` : undefined} />
        <Stat label="Kalshi trades" value={n('kalshi_trades')} />
        <Stat label="Kalshi book snapshots" value={n('kalshi_book')} />
        <Stat label="Spot updates" value={n('spot')} />
        <Stat label="BRTI index ticks" value={n('brti')} sub={rec.brti ? 'settlement stream on' : 'needs Kalshi API key'} />
        <Stat label="Polymarket US books" value={n('pmus_book')} sub={rec.pmus_book ? 'arbitrage data on' : 'waiting for listed windows'} />
        <Stat label="Bot" value={r.bot ? `${r.bot.mode} ${r.bot.pnl >= 0 ? '+' : ''}${usd(r.bot.pnl)}` : 'off'} sub={r.bot ? `${r.bot.trade_count} trades` : undefined} />
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="num w-full text-left font-mono text-sm">
          <thead className="text-xs text-[var(--ink-3)]">
            <tr><th className="py-1 pr-4">candidate strategy</th><th className="pr-4">windows</th><th className="pr-4">P&amp;L/contract last 14d</th><th className="pr-4">prior 14d</th><th className="pr-4">t-stat</th><th>verdict</th></tr>
          </thead>
          <tbody>
            {(r.loop?.results ?? []).map((x, i) => (
              <tr key={i} className="border-t border-[var(--line)]">
                <td className="py-1 pr-4 font-sans">{x.candidate}</td>
                <td className="pr-4">{x.error ? 'error' : x.windows}</td>
                <td className="pr-4" style={{ color: (x.pnl_recent ?? 0) > 0 ? 'var(--above)' : 'var(--below)' }}>{c(x.pnl_recent)}</td>
                <td className="pr-4" style={{ color: (x.pnl_prior ?? 0) > 0 ? 'var(--above)' : 'var(--below)' }}>{c(x.pnl_prior)}</td>
                <td className="pr-4">{x.t_stat?.toFixed(2) ?? '–'}</td>
                <td style={{ color: x.promote ? 'var(--above)' : 'var(--ink-3)' }}>{x.promote ? 'PROMOTE' : (x.windows ?? 0) < 300 ? 'collecting' : 'rejected'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-[var(--ink-3)]">A strategy is promoted only with ≥300 windows, t-stat ≥ 2, and positive P&amp;L after fees in both of the last two 14-day periods. Re-scored every 6 hours.</p>
      </div>
    </Panel>
  );
}

// ─── page ────────────────────────────────────────────────────────────
export default function App() {
  const sig = usePoll<Signal>('/signal', 1000);
  const series = usePoll<{ snapshots: Snapshot[]; bars: Bar[] }>('/series', 5000);
  const history = usePoll<WindowRow[]>('/history?limit=32', 15000);
  const pf = usePoll<Portfolio>('/portfolio', 3000);
  const research = usePoll<Research>('/research', 30000);
  const ok = sig?.status === 'ok';
  const month = sig?.month;
  const side = sig?.side;
  const color = !ok || sig?.state !== 'LOCKED' ? 'var(--ink-2)' : side === 'ABOVE' ? 'var(--above)' : 'var(--below)';
  const conf = sig?.prob_above == null ? null : side === 'ABOVE' ? (sig.locked_prob ?? sig.prob_above) : 1 - (sig.locked_prob ?? sig.prob_above);
  const edge = side === 'ABOVE' ? sig?.edge_above : sig?.edge_below;
  const headline = !ok ? (sig?.status ?? 'connecting').replace('_', ' ') : sig!.state === 'LOCKED' ? side : sig!.state === 'NO_CALL' ? 'NO CALL' : 'WAIT';

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-4 py-6">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold">BTC 15-minute · Kalshi {sig?.ticker && <span className="font-mono text-sm text-[var(--ink-3)]">{sig.ticker}</span>}</h1>
        <div className="text-xs text-[var(--ink-3)]">model {sig?.model ?? '–'} · lock at {pct(sig?.tau, 0)} · ≥{sig?.min_lead_s ?? '–'}s left</div>
      </header>

      <div className="grid gap-4 md:grid-cols-3">
        <Panel className="md:col-span-1">
          <div className="text-xs uppercase tracking-wider text-[var(--ink-3)]">window {et(sig?.window_open)}–{et(sig?.window_close)} ET</div>
          <div className="mt-2 flex items-baseline gap-3">
            <span className="text-5xl font-bold" style={{ color }}>
              {sig?.state === 'LOCKED' ? (side === 'ABOVE' ? '▲ ' : '▼ ') : ''}{headline}
            </span>
            {sig?.state === 'LOCKED' && <span aria-label="locked">🔒</span>}
          </div>
          <div className="num mt-1 font-mono text-2xl text-[var(--ink-2)]">{clock(sig?.secs_left)} left</div>
          <div className="mt-3 text-sm text-[var(--ink-2)]">
            {sig?.state === 'LOCKED' ? `Locked ${side} at ${pct(conf)} with ${clock(sig.locked_secs_left)} left.`
              : sig?.state === 'NO_CALL' ? 'Never reached the confidence threshold in time. Skip this window.'
              : ok ? `Leaning ${sig!.prob_above! >= 0.5 ? 'ABOVE' : 'BELOW'} at ${pct(Math.max(sig!.prob_above!, 1 - sig!.prob_above!))}; locks at ${pct(sig!.tau, 0)}.` : 'Waiting for data.'}
          </div>
          {ok && edge != null && (
            <div className="mt-2 text-sm text-[var(--ink-3)]">
              Model vs Kalshi ask: {edge >= 0 ? '+' : ''}{(edge * 100).toFixed(1)}¢ after fee (unverified: so far Kalshi's own price is the better forecast)
            </div>
          )}
        </Panel>

        <Panel title="Now" className="md:col-span-2" grid="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="BTC (Coinbase)" value={usd(sig?.price)} sub={sig?.distance_pct == null ? undefined : `${sig.distance_pct >= 0 ? '+' : ''}${sig.distance_pct.toFixed(3)}% vs target`} />
          <Stat label="Target" value={usd(sig?.target)} sub={sig?.target_source} />
          <Stat label="P(above)" value={pct(sig?.prob_above)} sub={`model ${sig?.model ?? '–'}`} />
          <Stat label="Kalshi YES / NO ask" value={`${cents(sig?.yes_ask)} / ${cents(sig?.no_ask)}`} sub={sig?.yes_bid != null ? `YES bid ${cents(sig.yes_bid)}` : 'no quote'} />
        </Panel>
      </div>

      {pf && pf.mode !== 'off' && <PortfolioPanel pf={pf} />}
      {research && <ResearchPanel r={research} />}

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Last hour (1-minute candles)"><Candles bars={series?.bars ?? []} target={sig?.target} price={sig?.price} /></Panel>
        <Panel title="P(above) this window"><ProbChart snaps={series?.snapshots ?? []} tau={sig?.tau ?? 0.8} lockedSecsLeft={sig?.locked_secs_left} /></Panel>
      </div>

      <Panel title={`Live month ${month?.period ?? ''}`} grid="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Accuracy on calls" value={pct(month?.accuracy)} sub={month?.acc_ci_low != null ? `95% CI ${pct(month.acc_ci_low)}–${pct(month.acc_ci_high)}` : 'no settled calls yet'} />
        <Stat label="Correct / settled" value={`${month?.correct ?? 0} / ${month?.settled_calls ?? 0}`} />
        <Stat label="Coverage" value={pct(month?.coverage, 0)} sub={`${month?.calls ?? 0} calls of ${month?.windows_eligible ?? 0} windows`} />
        <Stat label="Target" value="75%" sub={month?.acc_ci_low != null ? (month.acc_ci_low >= 0.75 ? 'CI clears 75%' : 'not yet conclusive') : undefined} />
        <Stat label="P&L, 1 contract at lock ask" value={month?.pnl_1_contract == null ? '–' : `$${month.pnl_1_contract.toFixed(2)}`} />
      </Panel>

      <Panel title="Recent windows">
        <div className="overflow-x-auto">
          <table className="num w-full text-left font-mono text-sm">
            <thead className="text-xs text-[var(--ink-3)]">
              <tr><th className="py-1 pr-4">ET</th><th className="pr-4">target</th><th className="pr-4">call</th><th className="pr-4">conf.</th><th className="pr-4">locked</th><th className="pr-4">paid</th><th className="pr-4">result</th><th>✓</th></tr>
            </thead>
            <tbody>
              {(history ?? []).map(r => {
                const hit = r.locked_side && r.result ? (r.locked_side === 'ABOVE') === (r.result === 'yes') : null;
                const c = r.locked_prob == null ? null : r.locked_side === 'ABOVE' ? r.locked_prob : 1 - r.locked_prob;
                const paid = r.locked_side === 'ABOVE' ? r.yes_ask_at_lock : r.locked_side === 'BELOW' ? r.no_ask_at_lock : null;
                return (
                  <tr key={r.window_open} className="border-t border-[var(--line)]">
                    <td className="py-1 pr-4">{et(r.window_open)}</td>
                    <td className="pr-4">{r.target?.toFixed(2)}</td>
                    <td className="pr-4" style={{ color: r.locked_side === 'ABOVE' ? 'var(--above)' : r.locked_side === 'BELOW' ? 'var(--below)' : 'var(--ink-3)' }}>{r.locked_side ?? 'no call'}</td>
                    <td className="pr-4">{pct(c, 0)}</td>
                    <td className="pr-4">{r.locked_secs_left == null ? '–' : `${clock(r.locked_secs_left)} left`}</td>
                    <td className="pr-4">{cents(paid)}</td>
                    <td className="pr-4">{r.result ?? 'pending'}</td>
                    <td>{hit == null ? '' : hit ? '✓ right' : '✗ wrong'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
      <footer className="pb-4 text-xs text-[var(--ink-3)]">Research tool. Signals are advisory; Kalshi's official result is ground truth.</footer>
    </div>
  );
}
