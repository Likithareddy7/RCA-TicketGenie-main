// One turn in the conversation: a customer bubble, or an assistant answer with its
// steps, source tickets, ticket details and outcome.
import { useState } from 'react'

// Steps are rendered uniformly. There is deliberately no colour coding and no badge
// distinguishing the steps our team performs: the wording already carries it.
function StepList({ steps }) {
  if (!steps?.length) return null
  return (
    <ol className="mt-2 space-y-2">
      {steps.map((s, i) => (
        <li key={i} className="flex gap-3">
          <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-bold text-slate-500">
            {i + 1}
          </span>
          <p className="min-w-0 text-sm leading-relaxed text-slate-700">{s.text}</p>
        </li>
      ))}
    </ol>
  )
}

// The per-ticket breakdown, hidden behind the dropdown. Each group was generated from
// that one ticket alone, and its recorded resolution is shown underneath so the
// generated steps can be checked against the source directly.
function Breakdown({ groups, count }) {
  const [open, setOpen] = useState(false)
  const n = count || groups?.length || 0
  if (!n) return null

  return (
    <div className="mt-3 border-t border-slate-100 pt-2.5">
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={!groups?.length}
        className="flex items-center gap-1.5 text-[11px] text-slate-400 transition hover:text-slate-600 disabled:cursor-default"
      >
        <svg viewBox="0 0 24 24" fill="none" className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`}>
          <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Based on {n} similar resolved {n === 1 ? 'case' : 'cases'}
      </button>

      {open && groups?.length > 0 && (
        <div className="mt-2.5 space-y-2.5">
          {groups.map((g, i) => (
            <div key={g.number || i} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs font-bold text-slate-700">{g.number}</span>
                  {g.subcategory && (
                    <span className="rounded bg-white px-1.5 py-0.5 text-[10px] text-slate-500 ring-1 ring-inset ring-slate-200">
                      {g.subcategory}
                    </span>
                  )}
                  {g.state && <span className="text-[10px] text-slate-400">{g.state}</span>}
                </div>
                <div className="flex items-center gap-2">
                  <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
                    <div className="h-full rounded-full bg-slate-400"
                         style={{ width: `${Math.round((g.similarity || 0) * 100)}%` }} />
                  </div>
                  <span className="w-9 text-right text-xs font-semibold tabular-nums text-slate-600">
                    {Math.round((g.similarity || 0) * 100)}%
                  </span>
                </div>
              </div>

              {g.short_description && (
                <p className="mt-1.5 text-xs font-medium leading-relaxed text-slate-700">{g.short_description}</p>
              )}

              <dl className="mt-2 space-y-1.5">
                {g.resolution_code && (
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Code</dt>
                    <dd className="font-mono text-xs text-slate-600">{g.resolution_code}</dd>
                  </div>
                )}
                {g.root_cause && (
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Root cause</dt>
                    <dd className="text-xs leading-relaxed text-slate-600">{g.root_cause}</dd>
                  </div>
                )}
                {g.resolution && (
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Resolution</dt>
                    <dd className="text-xs leading-relaxed text-slate-600">{g.resolution}</dd>
                  </div>
                )}
              </dl>

              {g.steps?.length > 0 && (
                <div className="mt-2 border-t border-slate-200 pt-2">
                  <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                    Steps taken on this ticket
                  </p>
                  <ol className="mt-1.5 list-inside list-decimal space-y-1">
                    {g.steps.map((st, j) => (
                      <li key={j} className="text-xs leading-relaxed text-slate-600">{st.text}</li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// The ticket as it stands. A field the customer has not given reads as "still needed"
// rather than as a blank, so nothing looks quietly filled in.
function TicketFields({ rows, stage }) {
  if (!rows?.length) return null
  const anyValue = rows.some((r) => r.value)
  if (!anyValue && stage !== 'ticket_review') return null
  return (
    <dl className="mt-3 divide-y divide-slate-100 overflow-hidden rounded-lg border border-slate-200">
      {rows.map((r) => (
        <div key={r.key} className="grid grid-cols-3 gap-3 bg-white px-3 py-2">
          <dt className="col-span-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">{r.label}</dt>
          <dd className={`col-span-2 text-sm ${r.value ? 'text-slate-800' : 'italic text-slate-400'}`}>
            {r.value || 'still needed'}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function CreatedTicket({ ticket }) {
  if (!ticket?.number) return null
  return (
    <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3.5">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Your ticket reference</p>
      <p className="mt-1 font-mono text-base font-bold text-slate-800">{ticket.number}</p>
      {ticket.url && (
        <a
          href={ticket.url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-blue-600 hover:text-blue-700"
        >
          Open in ServiceNow
          <svg viewBox="0 0 24 24" fill="none" className="h-3 w-3">
            <path d="M14 5h5v5M19 5l-7 7M11 5H6a1 1 0 00-1 1v12a1 1 0 001 1h12a1 1 0 001-1v-5"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </a>
      )}
    </div>
  )
}

export default function ChatTurn({ message, onQuickReply, busy }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] whitespace-pre-line rounded-2xl rounded-br-md bg-blue-600 px-4 py-2.5 text-sm leading-relaxed text-white shadow-sm">
          {message.content}
        </div>
      </div>
    )
  }

  const t = message.turn || {}

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-sky-400 to-blue-600 text-white shadow-sm">
        <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
          <path d="M5 4h9l5 5v11a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1Z" stroke="currentColor" strokeWidth="1.6" />
          <path d="M9 13l2 2 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      <div className="min-w-0 flex-1">
        <div className="rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-3 shadow-sm">
          <p className="whitespace-pre-line text-sm leading-relaxed text-slate-700">{message.content}</p>

          {/* Two sections: what the customer can try, then what we would do if that
              does not work. Interleaving them made it unclear who was being asked
              to act. */}
          {t.steps?.length > 0 && (
            <>
              {t.customer_header && (
                <p className="mt-3 text-sm font-semibold text-slate-700">{t.customer_header}</p>
              )}
              <StepList steps={t.steps} />
            </>
          )}
          {t.no_customer_steps_note && (
            <p className="mt-3 text-sm text-slate-600">{t.no_customer_steps_note}</p>
          )}
          {t.provider_steps?.length > 0 && (
            <div className="mt-4 border-t border-slate-100 pt-3">
              {t.provider_header && (
                <p className="text-sm font-semibold text-slate-700">{t.provider_header}</p>
              )}
              <StepList steps={t.provider_steps} />
            </div>
          )}
          {t.closing_question && (
            <p className="mt-3 text-sm font-medium text-slate-700">{t.closing_question}</p>
          )}
          <TicketFields rows={t.ticket_fields} stage={t.stage} />
          <CreatedTicket ticket={t.created_ticket} />
          <Breakdown groups={t.resolutions} count={t.sources_count} />
        </div>

        {t.quick_replies?.length > 0 && (
          <div className="mt-2.5 flex flex-wrap gap-2">
            {t.quick_replies.map((qr) => (
              <button
                key={qr.action}
                disabled={busy}
                onClick={() => onQuickReply(qr)}
                className="rounded-full border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm transition hover:border-blue-400 hover:bg-blue-50 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {qr.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
