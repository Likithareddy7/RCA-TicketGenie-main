import { StateBadge, AgeBadge } from './ui'

// Surface the most identifying fields generically (skip routing/noise labels),
// so the queue works for any fallout type without hardcoding Location/Order.
const NOISE = new Set(['state', 'urgency', 'caller', 'assignment group', 'category', 'resolution code'])
const highlights = (t) =>
  (t.fields || []).filter((f) => !NOISE.has((f.label || '').toLowerCase())).slice(0, 2)

export default function QueueSidebar({ queue, selected, onSelect, loading, kbCount }) {
  return (
    <aside className="flex w-80 flex-shrink-0 flex-col border-r border-slate-200 bg-white">
      {/* Brand */}
      <div className="flex items-center gap-2.5 border-b border-slate-200 px-5 py-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-sky-400 to-blue-600 text-white shadow-sm">
          <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5">
            <path d="M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" strokeWidth="1.6" />
            <path d="M9 13l2 2 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <div>
          <p className="text-sm font-bold tracking-tight text-slate-800">TicketGenie</p>
          <p className="text-[11px] text-slate-400">Remediation Assistant</p>
        </div>
      </div>

      {/* Queue header */}
      <div className="flex items-center justify-between px-5 py-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Open Queue</span>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600">
          {queue.length}
        </span>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {loading && <p className="px-2 py-4 text-sm text-slate-400">Loading queue…</p>}
        {!loading && queue.length === 0 && (
          <p className="px-2 py-4 text-sm text-slate-400">No open fallout tickets.</p>
        )}
        <ul className="space-y-1.5">
          {queue.map((t) => {
            const active = selected === t.number
            return (
              <li key={t.number}>
                <button
                  onClick={() => onSelect(t.number)}
                  className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
                    active
                      ? 'border-blue-300 bg-blue-50'
                      : 'border-transparent hover:border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs font-semibold text-slate-700">{t.number}</span>
                    <div className="flex items-center gap-1.5">
                      <AgeBadge label={t.age_label} band={t.age_band} title={t.opened ? `Raised ${t.opened}` : ''} />
                      <StateBadge state={t.state} />
                    </div>
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs leading-snug text-slate-500">
                    {t.short_description}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-slate-400">
                    {highlights(t).map((f, i) => (
                      <span key={i} className="font-mono">
                        {f.value}
                        {i === 0 && highlights(t).length > 1 ? ' ·' : ''}
                      </span>
                    ))}
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      </div>

      {/* Footer */}
      <div className="border-t border-slate-200 px-5 py-3 text-[11px] text-slate-400">
        Knowledge base · {kbCount} resolved tickets
      </div>
    </aside>
  )
}
