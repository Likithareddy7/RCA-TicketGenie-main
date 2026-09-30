"""
Synthetic OPEN queue + matching BOSS OMS inventory
--------------------------------------------------
Writes two files that must agree with each other:

  data/synthetic_open_queue.json  — open tickets, in the ServiceNow export shape
                                    ({number, short_description, block, is_closed}),
                                    ready for seed_open_queue.py
  data/oms_data.json              — the inventory the deterministic checks read

They are generated together on purpose. A check can only name "which value is wrong"
if the ticket's BAN / order id actually exists in the inventory with a seeded
disagreement — generating the two separately is how you end up with a demo where
every ticket says "no applicable system check".

Every identifier is fabricated: BANs are 9-prefixed, order ids are XX9…, tasks are
OMTASK9…, telephone numbers use the reserved 555 exchange, names are invented.

Usage (from backend/):
    ../rca/bin/python make_synthetic_queue.py
"""

import os
import json

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
QUEUE_PATH = os.path.join(DATA, "synthetic_open_queue.json")
OMS_PATH = os.path.join(DATA, "oms_data.json")

GROUP = "BOSS OM IT Support"


def block(**kw) -> str:
    """Build the labelled comment block this app parses out of the journal."""
    lines = [f"Subcategory: {kw['sub']}",
             f"Business Service: {kw.get('service', 'BOSS OM')}",
             f"Assignment Group: {GROUP}",
             "Category: Marketing, Sales & Billing Applications",
             f"BAN: {kw['ban']}"]
    if kw.get("tn"):
        lines.append(f"TN: {kw['tn']}")
    if kw.get("order"):
        lines.append(f"Order Reference: {kw['order']}")
    if kw.get("task"):
        lines.append(f"Order Task: {kw['task']}")
    lines += ["Description:", kw["desc"], "Work Notes:", kw["notes"]]
    return "\n".join(lines)


# ── the queue ───────────────────────────────────────────────────────────
# Five Buy Flow tickets (the REDIRECT branch) and five BOSS OM tickets whose
# identifiers are seeded with a real disagreement (the COMMENT branch).

TICKETS = [
    # ---------- REDIRECT: Buy Flow ----------
    dict(number="SYN0001", sub="Digital Buyflow", service="BOSS AX Buyflow",
         ban="907431182", tn="(704) 555-2118", order="NC9204118337",
         sd="Remove TN (704) 555-2118 on BAN 907431182 and replace with TN (704) 555-2119",
         desc="DUE TO A BUY FLOW ORDER CAPTURE DEFECT STILL BEING PROCESSED WITH THE BUYFLOW "
              "TEAM PLEASE REMOVE TN (704) 555-2118 ON BAN 907431182 AND REPLACE WITH TN "
              "(704) 555-2119. DO NOT CLOSE THIS WITHOUT WORKING. WE CANNOT ADD NUMBERS TO AN "
              "ACCOUNT WITHOUT DISCONNECTING A CUSTOMER WHO ALREADY HAS SERVICE.",
         notes="Assigning to BOSS OM to update the service number for this customer."),
    dict(number="SYN0002", sub="Digital Buyflow", service="BOSS AX Buyflow",
         ban="913560447", tn="(615) 555-8043", order="TN9118004552",
         sd="Dana Whitfield 913560447 - remove TN (615) 555-8043 and replace with TN (615) 555-8044",
         desc="DUE TO A BUY FLOW DEFECT PLEASE REMOVE TN (615) 555-8043 ON BAN 913560447 AND "
              "REPLACE WITH TN (615) 555-8044. PLEASE DO NOT CLOSE THIS WITHOUT WORKING. WE "
              "CANNOT ADD NUMBERS TO AN ACCOUNT WITHOUT DISCONNECTING AN EXISTING CUSTOMER.",
         notes="Buy Flow produced the wrong service number at order capture. Please correct."),
    dict(number="SYN0003", sub="Digital Buyflow", service="BOSS AX Buyflow",
         ban="928104663", tn="(803) 555-6621", order="SC9330076104",
         sd="Remove TN (803) 555-6621 on BAN 928104663 and replace with TN (803) 555-6622",
         desc="ORDER CAPTURE IN BUYFLOW ASSIGNED THE WRONG TELEPHONE NUMBER. PLEASE REMOVE TN "
              "(803) 555-6621 ON BAN 928104663 AND REPLACE IT WITH TN (803) 555-6622.",
         notes="Referred from the digital channel; the capture defect sits with Buy Flow."),
    dict(number="SYN0004", sub="Digital Buyflow", service="BOSS AX Buyflow",
         ban="934778215", order="OH9412007733",
         sd="Change plan completed but changes are not effective - 934778215",
         desc="ORDER COMPLETED SOME WEEKS AGO HOWEVER THE SYSTEM IS STILL BILLING THE CUSTOMER. "
              "SERVICE PROFILE SHOWS ONLY PHONE, AND CHANGE PLAN WILL NOT ALLOW ANY CHANGES AS "
              "INTERNALLY THE SYSTEM DOES NOT RECOGNISE THE CHANGE. PLEASE CORRECT THE ACCOUNT.",
         notes="Raised through the Buy Flow digital journey; changes from the last order never applied."),
    dict(number="SYN0005", sub="Digital Buyflow", service="BOSS AX Buyflow",
         ban="941025590", tn="(504) 555-3390", order="LA9507113028",
         sd="Buy Flow order capture failed for BAN 941025590",
         desc="THE BUY FLOW ORDER FOR THIS CUSTOMER FAILED PART WAY THROUGH CAPTURE AND LEFT THE "
              "ACCOUNT WITH A PARTIAL SERVICE PROFILE. PLEASE ADVISE - WE CANNOT RE-RUN THE "
              "JOURNEY WITHOUT LOSING THE CUSTOMER'S EXISTING SERVICE.",
         notes="Escalated from the digital team; owned by Buy Flow, not BOSS OM."),

    # ---------- COMMENT: BOSS OM value checks ----------
    dict(number="SYN0010", sub="Network Type Mismatch", ban="952330871",
         sd="Wrong account banner - 952330871",
         desc="Asking for your assistance on correcting the banner from postpaid fiber to copper. "
              "The account shows the wrong banner in C360 and the agent cannot proceed.",
         notes="The provisioned network on this BAN is copper but the banner reads postpaid fiber. "
               "Please correct the banner so it matches what is actually provisioned."),
    dict(number="SYN0011", sub="Account Status Mismatch", ban="963118204",
         sd="No Disconnect Order but Account is Inactive - 963118204",
         desc="We are unable to close the OWS case due to BAN 963118204 having no disconnection "
              "order but already being Inactive. We also have no option to cancel the service, and "
              "per the notes on the account it was supposed to remain in service.",
         notes="No disconnect order exists on this BAN. Requesting the account status be reviewed."),
    dict(number="SYN0012", sub="Staging Stuck", ban="974602339", order="WI9613002247",
         sd="Order for speed upgrade not reflecting on the account - order WI9613002247",
         desc="Agent processed a speed upgrade from 100MBPS to 200MBPS plus VAS but it has not "
              "reflected on the customer account. Order number WI9613002247. The order was placed "
              "and accepted but the account still shows the old speed.",
         notes="Order does not appear in the order history on the account. Please push the order."),
    dict(number="SYN0013", sub="Account Status Mismatch", ban="985447016", order="NC9722034398",
         sd="Order cancelled but account still shows a pending order - 985447016",
         desc="The disconnect order on this account was cancelled but the account is still sitting "
              "in a pending state, so we cannot create a new order for the customer. Please review "
              "the account status against the order.",
         notes="Order NC9722034398 shows cancelled in BOSS but the account never came out of pending."),
    dict(number="SYN0014", sub="Staging Stuck", ban="996215483", order="OH9831082966",
         task="OMTASK930632918",
         sd="Order stuck past due date with an open task - order OH9831082966",
         desc="The install was completed on site but order OH9831082966 has not moved to complete. "
              "It is sitting past its due date and the customer is not being billed correctly.",
         notes="Tech confirmed the job is done. The order still has an open task blocking completion."),
]


# ── the inventory those tickets are checked against ──────────────────────
# Each entry seeds exactly one disagreement, so the check can name the wrong field.

ACCOUNTS = {
    # Buy Flow BANs: nothing is wrong in OMS. The defect is upstream in order
    # capture, which is precisely why those tickets redirect instead of remediating.
    "907431182": dict(account_status="Active", network_type="Fiber", banner="Fiber",
                      _note="Buy Flow ticket's BAN — OMS is healthy; the defect is upstream."),
    "913560447": dict(account_status="Active", network_type="Fiber", banner="Fiber"),
    "928104663": dict(account_status="Active", network_type="Copper", banner="Copper"),
    "934778215": dict(account_status="Active", network_type="Fiber", banner="Fiber"),
    "941025590": dict(account_status="Active", network_type="Fiber", banner="Fiber"),

    "952330871": dict(account_status="Active", network_type="Copper", banner="Postpaid Fiber",
                      _note="Banner disagrees with network_type -> Network Type Issue."),
    "963118204": dict(account_status="Inactive", network_type="Copper", banner="Copper",
                      _note="Inactive with NO disconnect order on file -> should be Active."),
    "974602339": dict(account_status="Active", network_type="Fiber", banner="Fiber"),
    "985447016": dict(account_status="Pending", network_type="Fiber", banner="Fiber",
                      _note="Order cancelled but the account never left Pending."),
    "996215483": dict(account_status="Pending", network_type="Fiber", banner="Fiber"),
}

ORDERS = {
    "WI9613002247": dict(ban="974602339", order_type="Change", order_status="Staged",
                         reflected_on_account=False, stage="Staging", open_tasks=[],
                         _note="Classic Staging Stuck: order exists but never surfaced on the account."),
    "NC9722034398": dict(ban="985447016", order_type="Disconnect", order_status="Cancelled",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
    "OH9831082966": dict(ban="996215483", order_type="Add", order_status="InProgress",
                         reflected_on_account=True, stage="Order Completion",
                         open_tasks=[dict(task_id="OMTASK930632918",
                                          description="Wait for Job Completion",
                                          status="Open", blocking=True)],
                         _note="Past due with a blocking task -> Task Closure."),
    # Buy Flow orders exist and are healthy — proof the redirect is not hiding a defect.
    "NC9204118337": dict(ban="907431182", order_type="Change", order_status="Completed",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
    "TN9118004552": dict(ban="913560447", order_type="Change", order_status="Completed",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
    "SC9330076104": dict(ban="928104663", order_type="Change", order_status="Completed",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
    "OH9412007733": dict(ban="934778215", order_type="Change", order_status="Completed",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
    "LA9507113028": dict(ban="941025590", order_type="Add", order_status="Completed",
                         reflected_on_account=True, stage="Closed", open_tasks=[]),
}


def main():
    tickets = []
    for t in TICKETS:
        tickets.append({
            "number": t["number"],
            "short_description": t["sd"],
            "state_display": "New",
            "is_closed": False,
            "_source": "Synthetic demo ticket (backend/make_synthetic_queue.py). "
                       "All identifiers fabricated.",
            "block": block(**t),
        })
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump({"_description": "Synthetic OPEN fallout tickets for seeding into "
                                   "ServiceNow. Every identifier is fabricated.",
                   "enabled": False, "tickets": tickets}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(tickets)} open tickets -> {QUEUE_PATH}")

    oms = {
        "_description": "Synthetic BOSS/OMS inventory backing the value-checks, generated "
                        "alongside data/synthetic_open_queue.json by "
                        "backend/make_synthetic_queue.py. Every identifier is fabricated. "
                        "Read fresh on every call (backend/oms_store.py).",
        "_how_checks_use_it": "Each check compares two values that are SUPPOSED to agree "
                              "(account status vs its orders, banner vs provisioned network "
                              "type, order stage vs whether it reflects on the account) and "
                              "reports which one is wrong. Nothing is written back.",
        "accounts": {b: {"ban": b, **v} for b, v in ACCOUNTS.items()},
        "orders": {o: {"order_id": o, **v} for o, v in ORDERS.items()},
    }
    with open(OMS_PATH, "w", encoding="utf-8") as f:
        json.dump(oms, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote inventory ({len(ACCOUNTS)} accounts, {len(ORDERS)} orders) -> {OMS_PATH}")

    # Every ticket identifier must resolve, or the check cannot name a field.
    missing = []
    for t in TICKETS:
        if t["ban"] not in ACCOUNTS:
            missing.append(f"{t['number']}: BAN {t['ban']}")
        if t.get("order") and t["order"] not in ORDERS:
            missing.append(f"{t['number']}: order {t['order']}")
    print("\nidentifier cross-check:", "ALL RESOLVE" if not missing else f"MISSING {missing}")


if __name__ == "__main__":
    main()
