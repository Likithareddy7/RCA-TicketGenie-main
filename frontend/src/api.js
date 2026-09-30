// Thin client for the FastAPI backend (backend/fallout_api.py). One function per
// endpoint the UI uses. Base URL is the local dev backend.
import axios from 'axios'

const API = 'http://localhost:8000'

export const http = axios.create({ baseURL: API })

export const getQueue = () => http.get('/fallout/tickets').then((r) => r.data)
export const getRecommendation = (number) =>
  http.get(`/fallout/recommend/${number}`).then((r) => r.data)
// Unused by the current UI, kept for parity with the backend endpoint.
export const getProvisioning = () => http.get('/fallout/provisioning').then((r) => r.data)
export const approve = (payload) => http.post('/fallout/approve', payload).then((r) => r.data)
export const getBreakdown = (number, against) =>
  http.post('/fallout/breakdown', { number, against }).then((r) => r.data)

// Free-text search over the closed-ticket KB. The same endpoint also accepts a
// bare incident number, in which case it returns the FULL queue-style
// recommendation (mode: 'ticket') instead of search results (mode: 'search').
export const searchTickets = (query) => http.post('/fallout/search', { query }).then((r) => r.data)
// Seed queries for the empty state, served by the backend so they stay in step
// with what the shipped knowledge base actually contains.
export const getExamples = () => http.get('/fallout/examples').then((r) => r.data)
export const getHealth = () => http.get('/fallout/health').then((r) => r.data)

// One turn of the customer conversation. Stateless: the whole transcript is sent
// each time, and `action` carries which quick reply was pressed.
export const sendChat = (messages, action = '') =>
  http.post('/fallout/chat', { messages, action }).then((r) => r.data)
