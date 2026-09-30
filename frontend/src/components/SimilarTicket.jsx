// One row in the "Similar historical tickets" list. Collapsed it shows the match
// (number, similarity, resolution code, recorded resolution); expanding it lazily
// fetches an AI breakdown of that historical ticket (symptom correlation + root
// cause + resolution steps) matched against the current open ticket.
import { useState } from 'react'
import { getBreakdown } from '../api'

function SimilarityBar({ value }) {
  const pct = Math.round((value || 0) * 100)
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-100">
        <div className="h-full rounded-full bg-blue-500" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-9 text-right text-xs font-medium text-slate-500">{pct}%</span>
    </div>
  )
}

function Section({ label, text, accent }) {
  if (!text) return null
  return (
    <div className={`rounded-md border-l-2 ${accent} bg-slate-50 px-3 py-2`}>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
      <p className="mt-0.5 text-xs leading-relaxed text-slate-600">{text}</p>
    </div>
  )
}

export default function SimilarTicket({ candidate: c, openNumber }) {
  const [open, setOpen] = useState(false)
  const [bd, setBd] = useState(null)
  const [loading, setLoading] = useState(false)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !bd) {
      setLoading(true)
      try {
        setBd(await getBreakdown(c.number, openNumber))
      } catch {
        setBd({ error: 'Failed to load breakdown.' })
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <li className="py-3">
      <button onClick={toggle} className="group w-full text-left">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              className={`h-3.5 w-3.5 text-slate-400 transition-transform ${open ? 'rotate-90' : ''}`}
            >
              <path d="M9 6l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="font-mono text-xs font-semibold text-slate-700">{c.number}</span>
            {c.location_id && <span className="text-xs text-slate-400">{c.location_id}</span>}
            <span className="rounded bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-500">{c.resolution_code}</span>
          </div>
          <SimilarityBar value={c.similarity} />
        </div>
        {c.resolution && (
          <p className="mt-1.5 pl-6 text-xs leading-relaxed text-slate-500">{c.resolution}</p>
        )}
        {!open && (
          <span className="mt-1 block pl-6 text-[10px] text-slate-400 group-hover:text-blue-500">
            Click to view full analysis
          </span>
        )}
      </button>

      {open && (
        <div className="mt-2 space-y-2 pl-6">
          {loading && (
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
              Generating analysis…
            </div>
          )}
          {bd?.error && <p className="text-xs text-rose-600">{bd.error}</p>}
          {bd && !bd.error && (
            <>
              {/* `source: record` means this was assembled from the ticket's own
                  recorded data rather than written by the model. Say so, so a
                  quoted record is never mistaken for tailored analysis. */}
              {bd.source === 'record' && (
                <p className="text-[10px] uppercase tracking-wide text-slate-400">
                  From this ticket&apos;s recorded data
                </p>
              )}
              <Section label="Symptom correlation" text={bd.symptom_correlation} accent="border-l-blue-500" />
              <Section label="Root cause" text={bd.root_cause} accent="border-l-amber-400" />
              {bd.steps?.length > 0 && (
                <div className="rounded-md border-l-2 border-l-emerald-500 bg-slate-50 px-3 py-2">
                  <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Resolution steps</p>
                  <ol className="mt-1 list-inside list-decimal space-y-1">
                    {bd.steps.map((s, i) => (
                      <li key={i} className="text-xs leading-relaxed text-slate-600">{s}</li>
                    ))}
                  </ol>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </li>
  )
}
