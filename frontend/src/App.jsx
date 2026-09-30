// Root component for the customer-facing remediation assistant.
//
// A conversation, not a search. It opens on a greeting with the input centred
// underneath, and once the first message is sent the same input pins to the bottom
// and the exchange builds above it.
//
// The flow the client asked for:
//   1. the customer describes a problem, and gets steps drawn from resolved tickets
//   2. if the steps work, the conversation is closed out
//   3. if they get stuck, or a step needs our engineer, we offer a ticket or a rep
//
// Intent comes from the quick-reply buttons rather than from parsing free text, so
// the backend's control flow stays deterministic. Accepting "open a ticket" returns
// a DRAFT: nothing is created in ServiceNow.
//
// Typing an incident number is an internal power path that still returns the full
// agent recommendation, including the approve and reassign bar. Customers never do
// this; it keeps the ServiceNow write paths reachable from the one input.
import { useEffect, useRef, useState } from 'react'
import ChatComposer from './components/ChatComposer'
import ChatTurn from './components/ChatTurn'
import RecommendationView from './components/RecommendationView'
import { sendChat, approve } from './api'

function Logo({ size = 'sm' }) {
  const box = size === 'lg' ? 'h-14 w-14 rounded-2xl' : 'h-9 w-9 rounded-lg'
  const icon = size === 'lg' ? 'h-7 w-7' : 'h-5 w-5'
  return (
    <div className={`flex items-center justify-center bg-gradient-to-br from-sky-400 to-blue-600 text-white shadow-md ${box}`}>
      <svg viewBox="0 0 24 24" fill="none" className={icon}>
        <path d="M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" strokeWidth="1.6" />
        <path d="M9 13l2 2 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  )
}

function Thinking() {
  return (
    <div className="flex gap-3">
      <div className="mt-0.5 h-8 w-8 shrink-0 rounded-lg bg-gradient-to-br from-sky-400 to-blue-600 opacity-60" />
      <div className="flex items-center gap-1.5 rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-3.5 shadow-sm">
        {[0, 150, 300].map((d) => (
          <span
            key={d}
            className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-300"
            style={{ animationDelay: `${d}ms` }}
          />
        ))}
      </div>
    </div>
  )
}

export default function App() {
  // messages: {role, content, turn?} where `turn` carries the assistant payload
  // (steps, quick replies, ticket draft) alongside the prose.
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [ticketRec, setTicketRec] = useState(null)

  const [posting, setPosting] = useState(false)
  const [posted, setPosted] = useState(false)
  const [toast, setToast] = useState(null)

  const bottomRef = useRef(null)
  const started = messages.length > 0

  const showToast = (text, tone = 'ok') => {
    setToast({ text, tone })
    setTimeout(() => setToast(null), 3500)
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, busy])

  // One round trip. `userText` is what to show as the customer's bubble (a typed
  // message, or the label of the button they pressed); `action` is the quick reply.
  const turn = async (userText, action = '') => {
    if (busy) return
    const history = [...messages, { role: 'user', content: userText }]
    setMessages(history)
    setDraft('')
    setBusy(true)
    try {
      const payload = history.map((m) => ({ role: m.role, content: m.content }))
      const res = await sendChat(payload, action)

      if (res.stage === 'ticket_lookup') {
        setTicketRec(res)
        setPosted(false)
        setMessages([...history, {
          role: 'assistant',
          content: `Opening the full recommendation for ${res.ticket?.number || 'that incident'}.`,
          turn: res,
        }])
        return
      }

      setTicketRec(null)
      setMessages([...history, { role: 'assistant', content: res.reply, turn: res }])
    } catch {
      setMessages([...history, {
        role: 'assistant',
        content: 'I could not reach our systems just then. Please try again in a moment.',
        turn: { stage: 'escalate', quick_replies: [{ label: 'Talk to a representative', action: 'talk_to_rep' }] },
      }])
    } finally {
      setBusy(false)
    }
  }

  const onQuickReply = (qr) => turn(qr.label, qr.action)

  const reset = () => {
    setMessages([])
    setTicketRec(null)
    setDraft('')
    setPosted(false)
  }

  // Only reachable from the incident-number path, where a recommendation carries an
  // approvable comment.
  const onApprove = async (commentText) => {
    const number = ticketRec?.ticket?.number
    if (!number) return
    setPosting(true)
    try {
      const isRedirect = ticketRec.action === 'redirect'
      const res = await approve({
        number,
        comment: commentText,
        resolution_code: ticketRec.resolution_code,
        confidence: ticketRec.confidence,
        best_match: ticketRec.best_match?.number || '',
        reassign_to: isRedirect ? ticketRec.redirect?.assignment_group || '' : '',
      })
      if (res.success) {
        setPosted(true)
        showToast(
          res.reassigned?.reassigned
            ? `${number} reassigned to ${res.reassigned.assignment_group}.`
            : res.reassigned?.simulated
              ? `Demo ticket, ${number} not moved in ServiceNow.`
              : `Recommendation posted on ${number}.`
        )
      } else {
        showToast(res.error || 'Failed to post comment.', 'err')
      }
    } catch {
      showToast('Failed to post comment. Please try again.', 'err')
    } finally {
      setPosting(false)
    }
  }

  return (
    <div className="flex h-screen flex-col bg-slate-50 text-slate-900">
      {/* Greeting state: nothing but the welcome and the input, centred. */}
      {!started && (
        <div className="flex flex-1 flex-col items-center justify-center px-6">
          <div className="w-full max-w-2xl text-center">
            <div className="flex justify-center">
              <Logo size="lg" />
            </div>
            <h1 className="mt-6 text-2xl font-bold tracking-tight text-slate-800">
              Hello, how can I help?
            </h1>
            <div className="mt-8 text-left">
              <ChatComposer
                value={draft}
                onChange={setDraft}
                onSend={(t) => turn(t)}
                disabled={busy}
                autoFocus
                placeholder=""
              />
            </div>
          </div>
        </div>
      )}

      {/* Conversation state. */}
      {started && (
        <>
          <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/85 backdrop-blur">
            <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-3">
              <div className="flex items-center gap-2.5">
                <Logo />
                <div className="leading-tight">
                  <p className="text-sm font-bold tracking-tight text-slate-800">TicketGenie</p>
                  <p className="text-[11px] text-slate-400">Support Assistant</p>
                </div>
              </div>
              <button
                onClick={reset}
                className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:text-slate-800"
              >
                New conversation
              </button>
            </div>
          </header>

          <main className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-3xl space-y-5 px-6 py-6">
              {messages.map((m, i) => (
                <ChatTurn key={i} message={m} onQuickReply={onQuickReply} busy={busy} />
              ))}
              {busy && <Thinking />}

              {/* Internal power path: the full agent recommendation, with the
                  validation card and the approve and reassign bar. */}
              {ticketRec && !busy && (
                <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
                  <RecommendationView
                    number={ticketRec.ticket?.number || ''}
                    rec={ticketRec}
                    loading={false}
                    onApprove={onApprove}
                    posting={posting}
                    posted={posted}
                  />
                </div>
              )}

              <div ref={bottomRef} />
            </div>
          </main>

          <div className="border-t border-slate-200 bg-white/85 backdrop-blur">
            <div className="mx-auto max-w-3xl px-6 py-4">
              <ChatComposer
                value={draft}
                onChange={setDraft}
                onSend={(t) => turn(t)}
                disabled={busy}
                placeholder="Reply, or describe another problem"
              />
            </div>
          </div>
        </>
      )}

      {toast && (
        <div
          className={`fixed bottom-24 left-1/2 z-50 -translate-x-1/2 rounded-xl px-6 py-3 text-sm font-medium text-white shadow-2xl ${
            toast.tone === 'err' ? 'bg-rose-600' : 'bg-emerald-600'
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  )
}
