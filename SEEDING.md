# Seeding fallout tickets into a ServiceNow instance

All fallout tickets live as `incident` records **inside** ServiceNow — they are
not in the repo. A fresh clone has no tickets until you create them in your own
instance. These two scripts copy one instance's tickets into another.

`data/servicenow_seed.json` is a portable snapshot of the source instance's
fallout tickets (short_description + the labelled comment block + open/closed
state). It is committed to the repo.

## For the person receiving the tickets (import)

1. Put **your own** ServiceNow developer-instance credentials in `backend/.env`:

   ```
   SERVICENOW_INSTANCE=https://devXXXXXX.service-now.com
   SERVICENOW_USER=admin
   SERVICENOW_PASSWORD=your-password
   ```

2. From `backend/`, run the seeder with the repo's venv:

   ```powershell
   ..\rca\Scripts\python.exe seed_servicenow.py
   ```

   It creates each ticket in your instance, transitions the closed ones to
   Closed/Resolved, and **skips anything already present** (safe to re-run).

3. Start the app — the queue and knowledge base are now populated from your
   own instance.

## For the person sending the tickets (export — already done)

From `backend/`, with the SOURCE instance creds in `backend/.env`:

```powershell
..\rca\Scripts\python.exe export_tickets.py
```

This regenerates `data/servicenow_seed.json`. Commit and push it.

## Notes

- Read-only on the source; the export never modifies your instance.
- The seeder's only writes are creating incidents and closing the closed ones —
  it does not touch any pre-existing tickets.
- `close_code` defaults to `Solved (Permanently)`. If your instance uses a
  different close-code choice list and a close fails, the error is printed per
  ticket; adjust the value in `seed_servicenow.py`.
