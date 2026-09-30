"""
Export the vector DB to Excel
-----------------------------
Dumps everything ChromaDB actually holds for the knowledge base, so the indexed
data can be inspected without a Python session.

This is NOT a copy of the source spreadsheet. It is what survived parsing: the
identifiers recovered by regex from free text, the resolution code extracted from
the `{RCA TAG : ...}` marker, and — importantly — the exact `document` string that
gets embedded. Comparing that document column against the source is the quickest
way to see why a ticket does or does not retrieve well.

Sheets:
  KB tickets   one row per indexed ticket, with the embedded document
  Embedded doc the four problem-side fields, isolated, to show what retrieval sees
  Summary      counts by subcategory and resolution code, plus field coverage

Usage (from backend/):
    ../rca/bin/python export_kb.py [output.xlsx]
"""

import os
import sys
import collections

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

import fallout_store   # noqa: E402  (must follow load_dotenv)

DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "..",
                           "data", "kb_vector_db_export.xlsx")

# Ordered for reading: identity first, then what retrieval keys on, then payload.
COLUMNS = ["number", "subcategory", "resolution_code", "state",
           "ban", "order_ref", "task_ref", "tn", "location_id",
           "service_type", "category", "short_description", "description",
           "resolution_notes", "work_notes", "doc_hash"]


def fetch() -> tuple:
    col = fallout_store.get_collection()
    got = col.get(include=["metadatas", "documents"])
    ids = got.get("ids", [])
    metas = got.get("metadatas") or []
    docs = got.get("documents") or []
    return ids, metas, docs


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    ids, metas, docs = fetch()
    print(f"vector DB holds {len(ids)} indexed tickets")

    rows = []
    for _id, meta, doc in zip(ids, metas, docs):
        meta = meta or {}
        row = {"chroma_id": _id}
        row.update({c: meta.get(c, "") for c in COLUMNS})
        row["embedded_document"] = doc or ""
        row["embedded_chars"] = len(doc or "")
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("number").reset_index(drop=True)

    # What retrieval actually sees. The resolution is deliberately NOT here — it is
    # the answer, and embedding it would break query/document symmetry.
    embedded = df[["number", "short_description", "description",
                   "subcategory", "service_type", "embedded_document"]].copy()

    def counts(series, label):
        c = collections.Counter(x if str(x).strip() else "(blank)" for x in series)
        return pd.DataFrame(sorted(c.items(), key=lambda kv: -kv[1]),
                            columns=[label, "tickets"])

    coverage = pd.DataFrame(
        [{"field": c,
          "populated": int((df[c].astype(str).str.strip() != "").sum()),
          "of": len(df),
          "percent": round(100 * (df[c].astype(str).str.strip() != "").sum() / max(len(df), 1), 1)}
         for c in COLUMNS])

    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="KB tickets", index=False)
        embedded.to_excel(xl, sheet_name="Embedded doc", index=False)
        counts(df["subcategory"], "subcategory").to_excel(
            xl, sheet_name="Summary", index=False, startrow=0, startcol=0)
        counts(df["resolution_code"], "resolution_code").to_excel(
            xl, sheet_name="Summary", index=False, startrow=0, startcol=3)
        coverage.to_excel(xl, sheet_name="Summary", index=False,
                          startrow=0, startcol=7)

        # Readable column widths — the text columns are long.
        widths = {"KB tickets": [22, 12, 22, 34, 10, 12, 14, 14, 16, 14, 16, 26,
                                 46, 60, 60, 46, 34, 60, 14],
                  "Embedded doc": [12, 52, 70, 22, 18, 80],
                  "Summary": [26, 9, 4, 38, 9, 4, 4, 22, 11, 6, 9]}
        for sheet, ws in xl.sheets.items():
            for i, w in enumerate(widths.get(sheet, []), start=1):
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
            ws.freeze_panes = "A2"

    print(f"wrote {os.path.abspath(out)}")
    print(f"  sheets: KB tickets ({len(df)} rows), Embedded doc, Summary")


if __name__ == "__main__":
    main()
