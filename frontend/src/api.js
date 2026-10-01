// Thin client for the FastAPI backend (backend/fallout_api.py). One function per
// endpoint the UI actually uses. Base URL is the local dev backend.
import axios from 'axios'

const API = 'http://localhost:8000'

export const http = axios.create({ baseURL: API })

// One turn of the customer conversation. Stateless: the whole transcript is sent
// each time, and `action` carries which quick reply was pressed. A bare incident
// number in the message returns the full agent recommendation instead.
export const sendChat = (
  messages,
  action = '',
  triedSteps = [],
  suggestedSteps = [],
  knownFields = {},
) =>
  http
    .post('/fallout/chat', {
      messages,
      action,
      tried_steps: triedSteps,
      suggested_steps: suggestedSteps,
      known_fields: knownFields,
    })
    .then((r) => r.data)

// Posts the approved recommendation back to ServiceNow. Only reachable from the
// incident-number path, which is internal rather than customer facing.
export const approve = (payload) => http.post('/fallout/approve', payload).then((r) => r.data)

// Per-ticket breakdown, used when expanding a historical match on that same path.
export const getBreakdown = (number, against) =>
  http.post('/fallout/breakdown', { number, against }).then((r) => r.data)

export const getHealth = () => http.get('/fallout/health').then((r) => r.data)
