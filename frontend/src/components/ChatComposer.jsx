// The message input. Used in two places with the same code: centred under the
// greeting before the conversation starts, and pinned to the bottom once it has.
import { useRef, useEffect } from 'react'

export default function ChatComposer({ value, onChange, onSend, disabled, autoFocus, placeholder }) {
  const ref = useRef(null)

  // Grow with the text rather than scrolling inside a one-line box, capped so a
  // long paste cannot push the conversation off screen.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  const submit = (e) => {
    e.preventDefault()
    if (!value.trim() || disabled) return
    onSend(value)
  }

  const onKeyDown = (e) => {
    // Enter sends, Shift+Enter makes a new line, which is what people expect of a
    // chat box rather than of a form field.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit(e)
    }
  }

  return (
    <form onSubmit={submit} className="relative">
      <textarea
        ref={ref}
        rows={1}
        value={value}
        autoFocus={autoFocus}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder ?? 'Describe what is going wrong'}
        className="w-full resize-none rounded-2xl border-2 border-blue-200 bg-white py-4 pl-5 pr-14 text-[15px] leading-relaxed text-slate-800 shadow-lg shadow-blue-500/5 ring-4 ring-blue-500/10 outline-none transition-all placeholder:text-slate-400 hover:border-blue-300 focus:border-blue-500 focus:shadow-xl focus:shadow-blue-500/10 focus:ring-[6px] focus:ring-blue-500/20 disabled:bg-slate-50 disabled:text-slate-400"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        aria-label="Send message"
        className={`absolute bottom-3 right-3 flex h-9 w-9 items-center justify-center rounded-xl text-white transition-all ${
          disabled || !value.trim()
            ? 'cursor-not-allowed bg-slate-300'
            : 'bg-blue-600 shadow-md shadow-blue-600/30 hover:bg-blue-500'
        }`}
      >
        <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
          <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2.2"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
    </form>
  )
}
