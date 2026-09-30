import pandas as pd
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
WATCHLIST_CSV = os.path.join(DATA_DIR, "watchlist.csv")

WATCHLIST_COLUMNS = [
    "User_Name", "Email", "Phone", "Ticket_ID", "Target_Status", "Timestamp"
]


def init_watchlist_csv():
    """Create an empty watchlist.csv with headers if it doesn't exist."""
    if os.path.exists(WATCHLIST_CSV):
        return
    df = pd.DataFrame(columns=WATCHLIST_COLUMNS)
    df.to_csv(WATCHLIST_CSV, index=False)


def append_watchlist(user_name: str, email: str, phone: str, ticket_id: str, target_status: str) -> dict:
    """Add an entry to the watchlist."""
    init_watchlist_csv()
    now = datetime.now()

    entry = {
        "User_Name": user_name,
        "Email": email,
        "Phone": phone,
        "Ticket_ID": ticket_id,
        "Target_Status": target_status,
        "Timestamp": now.isoformat(),
    }

    df = pd.DataFrame([entry])
    df.to_csv(WATCHLIST_CSV, mode="a", header=False, index=False)

    return entry
