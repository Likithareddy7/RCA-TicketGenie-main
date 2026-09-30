// Thin client for the FastAPI backend (backend/fallout_api.py). One function per
// endpoint the UI uses. Base URL is the local dev backend.
import axios from 'axios'

const API = 'http://localhost:8000'

export const http = axios.create({ baseURL: API })

export const getQueue = () => http.get('/fallout/tickets').then((r) => r.data)
export const getRecommendation = (number) =>
  http.get(`/fallout/recommend/${number}`).then((r) => r.data)
// Unused by the current UI — kept for parity with the backend endpoint.
export const getProvisioning = () => http.get('/fallout/provisioning').then((r) => r.data)
export const approve = (payload) => http.post('/fallout/approve', payload).then((r) => r.data)
export const getBreakdown = (number, against) =>
  http.post('/fallout/breakdown', { number, against }).then((r) => r.data)
