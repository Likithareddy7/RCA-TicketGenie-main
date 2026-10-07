"""
Build the support knowledge base from the incident export
----------------------------------------------------------
Reads data/synthetic_incident_data.csv (the Business Hub support export) and
produces the KB spreadsheet the retrieval layer indexes.

Two things this does that are not obvious:

* **It dedupes to distinct problems.** The export holds hundreds of records but
  only a few dozen genuinely different problems: "Session expired error
  immediately after login" appears many times with different callers and always
  resolves the same way. Indexing every row would make a search return three
  copies of one answer, which defeats the grouped resolution view. One entry per
  distinct (short description template, resolution) pair keeps the comparison
  meaningful. The number of source records behind each entry is preserved, so
  nothing about the real frequency is lost.

* **It strips the per-ticket identifiers out of the short description** before
  comparing, because "Discount no longer applied to customer - 7000012834" and
  "... - 412612432" are the same problem. The trailing account number, order id,
  quote number and telephone number are replaced by a placeholder for the
  comparison, and the cleaned text is what gets embedded. Embedding a BAN helps
  no one: a customer searching will never type another customer's account number.

Output columns deliberately match the headers already pinned in
data/kb_column_map.json, so switching the KB to this file needs no mapping change.

Usage (from backend/):
    ../rca/bin/python make_support_kb.py
    ../rca/bin/python make_support_kb.py --all     # every row, no deduping
"""

import os
import re
import sys
from collections import Counter, OrderedDict

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SOURCE = os.path.join(DATA_DIR, "synthetic_incident_data.csv")
OUT = os.path.join(DATA_DIR, "support_kb.xlsx")
OUT_JSON = os.path.join(DATA_DIR, "support_kb.json")

# States that count as closed. The export uses both; both are resolved work.
CLOSED = {"closed", "resolved"}

# Identifiers that vary per ticket and must not take part in the comparison, nor
# be embedded. Ordered longest-pattern first so a phone number is not partly eaten
# by the account-number rule.
_SCRUB = [
    # A whole street address varies completely between tickets, so it is replaced
    # before anything else. Without this, three identical "qualifies for fiber"
    # problems would be indexed as three unrelated entries.
    (re.compile(r"^\d+\s+.+?\s+(?=qualifies for fiber)", re.I), "<address> "),
    # The county name varies the same way, and it is the only thing distinguishing
    # otherwise identical SSR billing tickets.
    (re.compile(r"\bCounty of\s+[A-Z][a-z]+", re.I), "County of <county>"),
    (re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}"), "<phone>"),
    (re.compile(r"\bQUO\d+\b"), "<quote>"),
    (re.compile(r"\bOrder I[Dd]\s*\d+\b"), "Order ID <order>"),
    (re.compile(r"\bSSR\s*\d+\b", re.I), "SSR <ssr>"),
    (re.compile(r"\b\d{6,12}\b"), "<account>"),
]


def normalise(text: str) -> str:
    """The comparable form of a short description, with identifiers removed."""
    s = " ".join(str(text or "").split())
    s = s.rstrip(" -").strip()
    for pattern, repl in _SCRUB:
        s = pattern.sub(repl, s)
    return " ".join(s.split())


def readable(text: str) -> str:
    """What a reader sees: identifiers removed, placeholders dropped entirely so the
    line reads as a problem statement rather than a template."""
    s = normalise(text)
    # Placeholders that stand in for a whole noun are replaced with that noun, so
    # the line still reads as a sentence. The rest are simply dropped.
    s = s.replace("County of <county> SSR <ssr>", "County SSR account")
    s = s.replace("County of <county>", "County")
    s = s.replace("<address>", "Address")
    for ph in ("<phone>", "<quote>", "<account>", "<ssr>", "<order>"):
        s = s.replace(ph, "")
    # A bare "Order ID -" or "County of" left at the front once its identifier is
    # gone reads as a fragment, so the sentence is tidied rather than left ragged.
    s = re.sub(r"^Order ID\s*-\s*", "", s)
    s = s.replace("County SSR account account is", "County SSR account is")
    # "<BAN> - Small Business" loses its only content once the account number goes.
    # Naming it as an account is the most the source actually says; anything more
    # would be inventing a problem statement the ticket never recorded.
    if s.strip(" -") == "Small Business":
        s = "Small Business account"
    s = s.replace("County of  SSR", "County of SSR").replace("County of ,", "County of")
    s = re.sub(r"\s*-\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip(" -").strip()
    # A few source tickets have nothing in the short description but an identifier,
    # so scrubbing leaves it empty. Name whichever identifier it was rather than
    # inventing a problem statement the ticket never recorded.
    if not s:
        key = normalise(text)
        for ph, noun in (("<quote>", "Quote reference"),
                         ("<order>", "Order reference"),
                         ("<account>", "Account reference")):
            if ph in key:
                return noun
        return ""
    return s


# The resolution notes carry per-ticket identifiers too, and they matter twice over.
#
# For GROUPING: "A new invite was sent to michele.williams@..." and the same line
# with another address are the same resolution, so leaving the email in splits one
# piece of knowledge across dozens of entries.
#
# For SAFETY: these notes are shown to customers. An email address or account
# number belonging to a different customer must never appear in a reply, so the
# scrub is not a tidiness measure, it is the same rule the customer-facing prompt
# enforces, applied at the source.
_RES_SCRUB = [
    (re.compile(r"[\w.+'-]+@[\w-]+\.[\w.]+"), "the email address on the account"),
    (re.compile(r"\bBAN\s+\d{6,12}\b", re.I), "the BAN provided"),
    (re.compile(r"\b\d{6,12}\b"), "the account provided"),
]


def clean_resolution(text: str) -> str:
    """Resolution notes with every per-ticket identifier replaced by a generic
    phrase. Used both as the grouping key and as what is shown."""
    s = " ".join(str(text or "").split())
    for pattern, repl in _RES_SCRUB:
        s = pattern.sub(repl, s)
    return " ".join(s.split())


def load(path: str) -> pd.DataFrame:
    # utf-8-sig strips the byte order mark the export carries, which would otherwise
    # leave the first column named "\ufeffNumber" and silently unmatchable.
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    return df


def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(f"Not found: {os.path.abspath(SOURCE)}\n"
                         f"Save the incident export there first.")
    df = load(SOURCE)
    need = ["Number", "Short description", "Resolution code", "Resolution notes",
            "Symptom", "Category", "State"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"The file is missing expected columns: {missing}\n"
                         f"Columns present: {list(df.columns)}")

    total = len(df)
    df = df[df["State"].str.strip().str.lower().isin(CLOSED)]
    closed = len(df)

    keep_all = "--all" in sys.argv
    groups = OrderedDict()
    for _, r in df.iterrows():
        sd = r["Short description"]
        resolution = clean_resolution(r["Resolution notes"])
        key = (normalise(sd), resolution) if not keep_all else r["Number"]
        g = groups.setdefault(key, {
            "numbers": [], "short_description": readable(sd),
            "resolution_code": r["Resolution code"].strip(),
            "resolution_notes": resolution,
            "symptom": r["Symptom"].strip(),
            "categories": Counter(), "states": Counter(),
        })
        g["numbers"].append(r["Number"].strip())
        g["categories"][r["Category"].strip()] += 1
        g["states"][r["State"].strip()] += 1

    rows = []
    for g in groups.values():
        # The representative incident number is the first one seen, so an entry can
        # always be traced back to a real record in the export.
        category = g["categories"].most_common(1)[0][0] if g["categories"] else ""
        rows.append({
            "number": g["numbers"][0],
            "state": "Closed",
            "short_description": g["short_description"],
            # No description column exists in this export. The symptom and the
            # resolution carry the meaning, so the description is left to the
            # symptom rather than invented.
            "description": g["symptom"],
            "u_issue_type": g["symptom"],
            "category": category,
            "close_notes": f"{g['resolution_notes']} {{RCA TAG : {g['resolution_code']}}}",
            "comments_and_work_notes": (
                f"Resolution code: {g['resolution_code']}. "
                f"Seen on {len(g['numbers'])} incident(s) in the export, "
                f"for example {', '.join(g['numbers'][:3])}."),
        })

    out = pd.DataFrame(rows)
    out.to_excel(OUT, index=False)
    out.to_json(OUT_JSON, orient="records", indent=2)

    print(f"source rows:        {total}")
    print(f"closed rows:        {closed}")
    print(f"KB entries written: {len(out)}"
          f"{'' if keep_all else '  (deduped to distinct problem + resolution)'}")
    print(f"  -> {os.path.abspath(OUT)}")
    print(f"  -> {os.path.abspath(OUT_JSON)}")
    print()
    print("issue types:")
    for k, v in Counter(out["u_issue_type"]).most_common():
        print(f"  {v:>3}  {k}")
    print()
    print("resolution codes:")
    codes = Counter(g["resolution_code"] for g in groups.values())
    for k, v in codes.most_common():
        print(f"  {v:>3}  {k}")
    print()
    print("first five entries as the KB will index them:")
    for _, r in out.head(5).iterrows():
        print(f"  [{r['u_issue_type']}] {r['short_description'][:66]}")
        print(f"      {r['close_notes'][:88]}")


if __name__ == "__main__":
    main()
