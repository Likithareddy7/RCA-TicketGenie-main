import { useState, useEffect } from 'react'
import { Card, Field, StateBadge, DecisionPill, AgeBadge } from './ui'
import SimilarTicket from './SimilarTicket'

function ValidationBadge({ status }) {
  const map = {
    confirmed: ['bg-emerald-50 text-emerald-700', 'Confirmed'],
    ambiguous: ['bg-amber-50 text-amber-700', 'Needs review'],
    not_confirmed: ['bg-slate-100 text-slate-500', 'Not confirmed'],
    not_applicable: ['bg-slate-100 text-slate-500', 'Not applicable'],
  }
  const [cls, text] = map[status] || map.not_confirmed
  return <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>{text}</span>
}

export default function RecommendationView({ number, rec, loading, onApprove, posting, posted }) {
  const [showEdit, setShowEdit] = useState(false)
  const [comment, setComment] = useState('')

  useEffect(() => {
    setComment(rec?.comment_preview || '')
    setShowEdit(false)
  }, [rec, number])

  if (!number) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-center text-slate-400">
        <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-100">
          <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7 text-slate-300">
            <path d="M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        </div>
        <p className="text-sm font-medium text-slate-500">Select a ticket</p>
        <p className="mt-1 text-xs">Choose a ticket from the queue to generate a remediation recommendation.</p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex items-center gap-3 text-slate-400">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
          <span className="text-sm">Analyzing {number} — retrieving matches & running validation checks…</span>
        </div>
      </div>
    )
  }

  if (rec?.error) return <div className="p-8 text-sm text-rose-600">{rec.error}</div>
  if (!rec) return null

  const t = rec.ticket || {}

  // Opens the real incident in ServiceNow. Rendered both before and after approval:
  // it is a plain link to live data, so clicking it after posting shows the comment
  // that was just written. Hidden for demo tickets, which have no sys_id.
  const viewInServiceNow = t.url ? (
    <a
      href={t.url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-800"
    >
      View in ServiceNow
      <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
        <path d="M14 5h5v5M19 5l-7 7M11 5H6a1 1 0 00-1 1v12a1 1 0 001 1h12a1 1 0 001-1v-5"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </a>
  ) : null
  const val = rec.validation || {}
  const best = rec.best_match || {}
  const isReview = rec.decision !== 'recommend'

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      {/* Ticket header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="font-mono text-lg font-bold text-slate-800">{t.number}</h2>
            <StateBadge state={t.state} />
            <AgeBadge label={t.age_label} band={t.age_band} title={t.opened ? `Raised ${t.opened}` : ''} />
          </div>
          <p className="mt-1 max-w-2xl text-sm text-slate-500">{t.short_description}</p>
        </div>
        {/* Only surface the pill when a ticket needs human review — the "ready"
            state is the default and would be redundant on every ticket. */}
        {rec.decision !== 'recommend' && <DecisionPill decision={rec.decision} />}
      </div>

      {/* Ticket details — rendered dynamically from whatever fields/sections exist */}
      <Card title="Ticket details">
        {t.fields?.length > 0 && (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
            {t.fields.map((f, i) => (
              <Field key={i} label={f.label} value={f.value} />
            ))}
          </dl>
        )}
        {t.sections?.length > 0 && (
          <div className="mt-4 space-y-3 border-t border-slate-100 pt-3">
            {t.sections.map((s, i) => (
              <div key={i}>
                <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{s.label}</p>
                <p className="mt-0.5 whitespace-pre-line text-sm leading-relaxed text-slate-600">{s.text}</p>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Resolution — combined from the top similar resolved tickets (history-driven).
          Always available; needs no system tool. */}
      {(rec.resolution?.per_ticket?.length > 0 || rec.resolution?.merged_summary) && (
        <Card title="Resolution">
          {rec.resolution_code && (
            <span className="inline-flex rounded-md bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700 ring-1 ring-inset ring-blue-600/20">
              {rec.resolution_code}
            </span>
          )}
          {rec.resolution?.merged_summary && (
            <p className="mt-3 text-sm leading-relaxed text-slate-700">{rec.resolution.merged_summary}</p>
          )}
          {rec.resolution?.per_ticket?.length > 0 && (
            <div className="mt-4 space-y-4 border-t border-slate-100 pt-3">
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                Combined from the top {rec.resolution.per_ticket.length} similar resolved tickets
              </p>
              {rec.resolution.per_ticket.map((pt, i) => (
                <div key={i}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-semibold text-slate-700">{pt.number}</span>
                    {pt.resolution_code && (
                      <span className="rounded bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-500">{pt.resolution_code}</span>
                    )}
                    <span className="text-[11px] text-slate-400">{Math.round((pt.similarity || 0) * 100)}% similar</span>
                  </div>
                  {pt.steps?.length > 0 && (
                    <ol className="mt-1.5 list-inside list-decimal space-y-1">
                      {pt.steps.map((s, j) => (
                        <li key={j} className="text-sm leading-relaxed text-slate-700">{s}</li>
                      ))}
                    </ol>
                  )}
                </div>
              ))}
            </div>
          )}
          {best.number && (
            <p className="mt-3 border-t border-slate-100 pt-3 text-xs text-slate-400">
              Closest historical match{' '}
              <span className="font-mono font-semibold text-slate-600">{best.number}</span> ·{' '}
              {Math.round((best.similarity || 0) * 100)}% similar
            </p>
          )}
        </Card>
      )}

      {/* Remediation — system-state validation + validated actions (tool-driven).
          When no tool applies, shows a neutral "Not applicable" note. */}
      <Card title="Remediation" right={<ValidationBadge status={val.status} />}>
        <p className="text-sm text-slate-600">{val.summary}</p>
        {rec.routed_tool && (
          <p className="mt-1 text-[11px] text-slate-400">
            Checked via <span className="font-mono text-slate-500">{rec.routed_tool}</span>
          </p>
        )}
        {val.details?.length > 0 && (
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 border-t border-slate-100 pt-3 sm:grid-cols-4">
            {val.details.map((d, i) => (
              <Field key={i} label={d.label} value={d.value} />
            ))}
          </dl>
        )}
        {rec.remediation_steps?.length > 0 && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Recommended actions</p>
            <ol className="mt-2 list-inside list-decimal space-y-1.5">
              {rec.remediation_steps.map((s, i) => (
                <li key={i} className="text-sm leading-relaxed text-slate-700">{s}</li>
              ))}
            </ol>
          </div>
        )}
        {rec.reason && <p className="mt-3 border-t border-slate-100 pt-3 text-xs italic text-slate-400">{rec.reason}</p>}
      </Card>

      {/* Similar historical tickets */}
      {rec.candidates?.length > 0 && (
        <Card title="Similar historical tickets">
          <ul className="divide-y divide-slate-100">
            {rec.candidates.map((c) => (
              <SimilarTicket key={c.number} candidate={c} openNumber={t.number} />
            ))}
          </ul>
        </Card>
      )}

      {/* Action bar */}
      <div className="sticky bottom-0 -mx-6 border-t border-slate-200 bg-white/90 px-6 py-4 backdrop-blur">
        {posted ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 rounded-lg bg-emerald-50 px-4 py-3 text-sm font-medium text-emerald-700">
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5">
                <path d="M5 13l4 4L19 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Recommendation posted as a comment on {t.number}. The ticket remains open for agent disposition.
            </div>
            {viewInServiceNow}
          </div>
        ) : (
          <div className="space-y-3">
            {showEdit && (
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                rows={8}
                className="w-full rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-400"
              />
            )}
            <div className="flex items-center justify-between">
              <button
                onClick={() => setShowEdit((s) => !s)}
                className="text-xs font-medium text-slate-500 hover:text-slate-700"
              >
                {showEdit ? 'Hide comment' : 'Review / edit comment'}
              </button>
              <div className="flex items-center gap-3">
                {isReview && (
                  <span className="text-xs text-amber-600">Needs human review — posting a review note (ticket stays open).</span>
                )}
                {viewInServiceNow}
                <button
                  onClick={() => onApprove(comment)}
                  disabled={posting}
                  className={`rounded-lg px-5 py-2.5 text-sm font-semibold text-white transition-colors ${
                    posting
                      ? 'cursor-not-allowed bg-slate-300'
                      : isReview
                        ? 'bg-amber-600 hover:bg-amber-500'
                        : 'bg-emerald-600 hover:bg-emerald-500'
                  }`}
                >
                  {posting ? 'Posting…' : isReview ? 'Post review note' : 'Approve & post comment'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
