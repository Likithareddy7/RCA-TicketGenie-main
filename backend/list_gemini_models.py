"""List the Gemini models this API key can call, and check that GEMINI_MODEL is one
of them. Read-only.

Model ids are retired and replaced on Google's own schedule, so the id pinned in
.env is worth verifying rather than trusting:

    ../rca/bin/python list_gemini_models.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("GEMINI_API_KEY", "").strip()
WANTED = os.getenv("GEMINI_MODEL", "gemini-3-flash").strip()


def main() -> int:
    if not KEY:
        print("GEMINI_API_KEY is not set in backend/.env.")
        print("Get one from https://aistudio.google.com/apikey")
        return 2

    try:
        from google import genai
    except ImportError:
        print("google-genai is not installed. pip install -r requirements.txt")
        return 2

    print(f"configured GEMINI_MODEL: {WANTED}\n")

    try:
        client = genai.Client(api_key=KEY)
        models = list(client.models.list())
    except Exception as e:
        print(f"Could not list models: {type(e).__name__}: {e}\n")
        print("Usual causes: the key is wrong, or the Generative Language API is not")
        print("enabled on the project behind it.")
        return 1

    names = []
    for m in models:
        # The API returns names like "models/gemini-3-flash"; the short id is what
        # generate_content takes.
        short = (getattr(m, "name", "") or "").split("/")[-1]
        if short:
            names.append(short)

    gemini = sorted(n for n in names if "gemini" in n.lower())
    print(f"{len(names)} models visible, {len(gemini)} of them Gemini:\n")
    for n in gemini:
        print(f"   {n}")

    print()
    if WANTED in names:
        print(f"OK: {WANTED!r} is available.")
        return 0
    print(f"WARNING: {WANTED!r} is NOT in the list above. Set GEMINI_MODEL in "
          f"backend/.env to one of them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
