// Small presentational primitives shared across the app.

export function ConfidenceBadge({ level }) {
  const map = {
    High: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    Medium: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    Low: 'bg-slate-100 text-slate-600 ring-slate-500/20',
  }
  const cls = map[level] || map.Low
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${cls}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {level || '—'} confidence
    </span>
  )
}

export function StateBadge({ state }) {
  const map = {
    New: 'bg-blue-50 text-blue-700 ring-blue-600/20',
    'In Progress': 'bg-violet-50 text-violet-700 ring-violet-600/20',
    Closed: 'bg-slate-100 text-slate-500 ring-slate-500/20',
  }
  const cls = map[state] || 'bg-slate-100 text-slate-600 ring-slate-500/20'
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${cls}`}>
      {state}
    </span>
  )
}

export function DecisionPill({ decision }) {
  const recommend = decision === 'recommend'
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${
        recommend ? 'bg-emerald-600 text-white' : 'bg-amber-500 text-white'
      }`}
    >
      {recommend ? 'Recommendation ready' : 'Needs human review'}
    </span>
  )
}

export function Field({ label, value, mono }) {
  return (
    <div>
      <dt className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className={`mt-0.5 text-sm text-slate-800 ${mono ? 'font-mono' : ''}`}>{value || '—'}</dd>
    </div>
  )
}

export function Card({ title, right, children, accent }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
      {title && (
        <header className={`flex items-center justify-between border-b border-slate-100 px-5 py-3 ${accent || ''}`}>
          <h3 className="text-sm font-semibold text-slate-700">{title}</h3>
          {right}
        </header>
      )}
      <div className="px-5 py-4">{children}</div>
    </section>
  )
}

// Ticket age. These incidents carry no usable ServiceNow priority field, so age is
// the queue's de-facto priority signal — the band is colour-coded so an overdue
// ticket is visible while scanning rather than only after opening it.
const AGE_STYLES = {
  fresh:    'bg-slate-100 text-slate-500 ring-slate-200',
  aging:    'bg-amber-50 text-amber-700 ring-amber-200',
  stale:    'bg-orange-100 text-orange-800 ring-orange-300',
  critical: 'bg-rose-100 text-rose-800 ring-rose-300',
}

export function AgeBadge({ label, band = 'fresh', title }) {
  if (!label) return null
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${
        AGE_STYLES[band] || AGE_STYLES.fresh
      }`}
    >
      <svg viewBox="0 0 24 24" fill="none" className="h-3 w-3">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
        <path d="M12 7v5l3 2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
      {label}
    </span>
  )
}
