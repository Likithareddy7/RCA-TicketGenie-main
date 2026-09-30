# Voice-to-Text Setup Guide

## What's Been Added

### Backend (Secure - API Key Protected)
- ✅ New `/transcribe` endpoint using OpenAI Whisper API
- ✅ API key stays secure on backend (no exposure in frontend)
- ✅ Supports audio transcription with high accuracy

### Frontend
- 🎤 Microphone icon in text input (right corner)
- 🔴 Red pulsing icon while recording
- 🟡 Yellow spinner while transcribing
- ✅ Transcribed text appears in input field

## Setup Instructions

### 1. Install New Backend Dependency
```bash
cd backend
pip install python-multipart
```

### 2. Create `.env` File (if not exists)
Create a file named `.env` in the `backend/` folder:
```
OPENAI_API_KEY=your_actual_openai_api_key_here
```

**Important:** Never commit the `.env` file to Git (already in .gitignore)

### 3. Restart Backend Server
```bash
cd backend
uvicorn main:app --reload
```

### 4. Restart Frontend Server
```bash
cd frontend
npm run dev
```

## How to Use

1. **Click the microphone icon** inside the text input (right side)
2. **Speak your issue** - the icon will turn red and pulse
3. **Click the mic again** to stop recording
4. **Wait for transcription** - icon turns yellow with spinner
5. **Review the text** that appears in the input
6. **Click Send** to submit your message

## Browser Requirements
- Works in: Chrome, Edge, Firefox, Safari
- Requires: Microphone permission (browser will ask)
- Requires: HTTPS in production (localhost works for dev)

## Security Notes
✅ OpenAI API key is stored on backend only
✅ No API keys exposed in frontend code
✅ Audio is sent securely to your backend
✅ Uses OpenAI Whisper for high-quality transcription

## Troubleshooting

**"Could not access microphone"**
- Grant microphone permission in browser settings

**"Transcription failed"**
- Check if backend server is running
- Verify OPENAI_API_KEY is set in backend/.env
- Check backend console for errors

**No response after recording**
- Check browser console (F12) for errors
- Verify backend is running on http://localhost:8000
