// Root component / state container for the remediation assistant.
// Two-pane layout: the open-fallout queue (left) and the recommendation for the
// selected ticket (right). Holds all app state and orchestrates the three calls
// the agent makes — load queue -> get recommendation -> approve (post comment).
// Human-in-the-loop: approving posts the EXACT text the agent saw/edited.
import { useEffect, useState, useCallback } from 'react'
import QueueSidebar from './components/QueueSidebar'
import RecommendationView from './components/RecommendationView'
import { getQueue, getRecommendation, approve } from './api'

export default function App() {
  const [queue, setQueue] = useState([])
  const [kbCount, setKbCount] = useState(0)
  const [queueLoading, setQueueLoading] = useState(true)

  const [selected, setSelected] = useState(null)
  const [rec, setRec] = useState(null)
  const [recLoading, setRecLoading] = useState(false)

  const [posting, setPosting] = useState(false)
  const [posted, setPosted] = useState(false)
  const [toast, setToast] = useState(null)

  const showToast = (text, tone = 'ok') => {
    setToast({ text, tone })
    setTimeout(() => setToast(null), 3500)
  }

  const loadQueue = useCallback(async () => {
    setQueueLoading(true)
    try {
      const data = await getQueue()
      setQueue(data.open || [])
      setKbCount(data.kb_count || 0)
    } catch {
      showToast('Could not load the queue. Is the backend running?', 'err')
    } finally {
      setQueueLoading(false)
    }
  }, [])

  useEffect(() => {
    loadQueue()
  }, [loadQueue])

  const onSelect = async (number) => {
    if (number === selected) return
    setSelected(number)
    setRec(null)
    setPosted(false)
    setRecLoading(true)
    try {
      setRec(await getRecommendation(number))
    } catch {
      setRec({ error: 'Failed to generate a recommendation. Please try again.' })
    } finally {
      setRecLoading(false)
    }
  }

  const onApprove = async (commentText) => {
    if (!selected || !rec) return
    setPosting(true)
    try {
      // Post the EXACT text the agent saw/edited — never regenerate on the server.
      // For a redirect, also carry the group the agent SAW: the server re-derives
      // the real target from the routing rules and refuses if the two disagree.
      const isRedirect = rec.action === 'redirect'
      const res = await approve({
        number: selected,
        comment: commentText,
        resolution_code: rec.resolution_code,
        confidence: rec.confidence,
        best_match: rec.best_match?.number || '',
        reassign_to: isRedirect ? rec.redirect?.assignment_group || '' : '',
      })
      if (res.success) {
        setPosted(true)
        showToast(
          res.reassigned?.reassigned
            ? `${selected} reassigned to ${res.reassigned.assignment_group}.`
            : res.reassigned?.simulated
              ? `Demo ticket — ${selected} not moved in ServiceNow.`
              : `Recommendation posted on ${selected}.`
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
    <div className="flex h-screen bg-slate-50 text-slate-900">
      <QueueSidebar
        queue={queue}
        selected={selected}
        onSelect={onSelect}
        loading={queueLoading}
        kbCount={kbCount}
      />

      <main className="flex-1 overflow-y-auto">
        <RecommendationView
          number={selected}
          rec={rec}
          loading={recLoading}
          onApprove={onApprove}
          posting={posting}
          posted={posted}
        />
      </main>

      {toast && (
        <div
          className={`fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl px-6 py-3 text-sm font-medium text-white shadow-2xl ${
            toast.tone === 'err' ? 'bg-rose-600' : 'bg-emerald-600'
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  )
}
