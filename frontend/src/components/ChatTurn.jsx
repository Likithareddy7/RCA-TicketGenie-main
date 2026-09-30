// One turn in the conversation: a customer bubble, or an assistant answer with its
// steps, quick replies and any ticket draft.
//
// This view is read by a CUSTOMER, so it deliberately shows no ticket numbers, no
// similarity percentages and no internal system names. The count of matched cases
// is surfaced as reassurance ("based on 3 similar resolved cases") without exposing
// what those cases were.

function StepList({ steps }) {
  if (!steps?.length) return null
  return (
    <ol className="mt-3 space-y-2.5">
      {steps.map((s, i) => (
        <li key={i} className="flex gap-3">
          <span
            className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
              s.needs_engineer ? 'bg-amber-100 text-amber-700' : 'bg-blue-100 text-blue-700'
            }`}
          >
            {i + 1}
          </span>
          <div className="min-w-0">
            <p className="text-sm leading-relaxed text-slate-700">{s.text}</p>
            {s.needs_engineer && (
              <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 ring-1 ring-inset ring-amber-600/20">
                Our team does this
              </span>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

function TicketDraft({ draft }) {
  if (!draft) return null
  return (
    <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3.5">
      <div className="flex items-center gap-2">
        <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4 text-slate-400">
          <path d="M5 4h9l5 5v11a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1Z"
                stroke="currentColor" strokeWidth="1.6" />
        </svg>
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
          Summary for our support team
        </p>
      </div>
      <p className="mt-2 text-sm font-medium text-slate-800">{draft.short_description}</p>
      {/* Stated plainly on purpose: nothing was filed, and the customer should not
          leave believing a ticket exists when it does not. */}
      <p className="mt-2 flex gap-1.5 text-[11px] leading-relaxed text-slate-500">
        <span className="mt-px shrink-0 text-amber-500">
          <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
            <path d="M12 8v4M12 16h.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </span>
        Not yet submitted. An agent reviews this before it is raised.
      </p>
    </div>
  )
}

export default function ChatTurn({ message, onQuickReply, busy }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-blue-600 px-4 py-2.5 text-sm leading-relaxed text-white shadow-sm">
          {message.content}
        </div>
      </div>
    )
  }

  const t = message.turn || {}
  const closed = t.stage === 'resolved'
  const handoff = t.stage === 'rep'

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-sky-400 to-blue-600 text-white shadow-sm">
        <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
          <path d="M5 4h9l5 5v11a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1Z" stroke="currentColor" strokeWidth="1.6" />
          <path d="M9 13l2 2 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      <div className="min-w-0 flex-1">
        <div
          className={`rounded-2xl rounded-tl-md border px-4 py-3 shadow-sm ${
            closed
              ? 'border-emerald-200 bg-emerald-50'
              : handoff
                ? 'border-violet-200 bg-violet-50'
                : 'border-slate-200 bg-white'
          }`}
        >
          <p className="whitespace-pre-line text-sm leading-relaxed text-slate-700">{message.content}</p>

          <StepList steps={t.steps} />
          <TicketDraft draft={t.ticket_draft} />

          {t.sources_count > 0 && (
            <p className="mt-3 border-t border-slate-100 pt-2.5 text-[11px] text-slate-400">
              Based on {t.sources_count} similar resolved {t.sources_count === 1 ? 'case' : 'cases'}
            </p>
          )}
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
