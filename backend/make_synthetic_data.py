"""
Synthetic BOSS OM fallout dataset
---------------------------------
Generates a demo-safe replacement for the real incidents export: same columns, same
fallout families in roughly the same proportions, same identifier *shapes* — but
every BAN, order id, telephone number, customer name and address is fabricated.

Why this exists: the real export carries live customer names, addresses, phone
numbers and account numbers. None of that belongs on a screen during a demo, and
retrieval quality does not depend on it — the problem-side text is what gets
embedded.

What is deliberately faithful (retrieval depends on it):
  * the fallout families and their relative frequency,
  * the phrasing agents actually use, including its messiness,
  * the `{RCA TAG : ...}` marker in close_notes, which IS the resolution code,
  * identifiers written inline rather than in columns, recovered by regex,
  * journal boilerplate in comments_and_work_notes, so _scrub_notes still earns its keep.

What is deliberately fake and recognisably so:
  * BANs start with 9   (real ones start 2/3/1000)
  * order ids are XX9…  (e.g. NC9100034521)
  * task ids are OMTASK9…
  * telephone numbers use the reserved 555 exchange
  * names are from a fixed invented list

Unlike the real export, close_notes here are written as NUMBERED ACTION STEPS, so a
reviewer can see what was actually done rather than a one-line sign-off.

Usage (from backend/):
    ../rca/bin/python make_synthetic_data.py            # writes the KB spreadsheet
    ../rca/bin/python make_synthetic_data.py --queue    # also writes open-queue JSON
"""

import os
import sys
import json
import random

import pandas as pd

random.seed(20260818)   # reproducible: same demo data every run

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
KB_PATH = os.path.join(DATA, "synthetic closed tickets.xlsx")
QUEUE_PATH = os.path.join(DATA, "synthetic_open_queue.json")
OMS_PATH = os.path.join(DATA, "oms_data.json")

STATES = ["NC", "TN", "AL", "OH", "WI", "PA", "MO", "NJ", "SC", "VA", "LA", "MS"]
NAMES = [
    "Dana Whitfield", "Marcus Ellery", "Priya Raman", "Tobias Kern", "Elena Marsh",
    "Grant Halloway", "Nadia Okonkwo", "Rafael Duarte", "Iris Chen", "Owen Pritchard",
    "Selma Vargas", "Desmond Clay", "Anya Petrov", "Colin Frasier", "Mira Sandoval",
    "Bennett Rowe", "Yara Haddad", "Elliot Vance", "Rosa Lindqvist", "Jonah Beckett",
]
CITIES = ["Fairmont", "Bridgeton", "Ashcombe", "Lakemoor", "Glenharbor",
          "Northvale", "Westbrook", "Stonefield", "Rivermede", "Oakhurst"]

CAT_SALES = "Marketing, Sales & Billing Applications"
CAT_CONTACT = "Contact Center Applications"
GROUP = "BOSS OM IT Support"
AGENTS = ["A. Nandakumar", "S. Bhatt", "R. Iyer", "M. Fernandes", "K. Subramanian",
          "P. Deshmukh", "L. Varghese"]

SPEEDS = [("100MBPS", "200MBPS"), ("200MBPS", "500MBPS"), ("500MBPS", "1GBPS"),
          ("50MBPS", "100MBPS"), ("300MBPS", "600MBPS")]


# ── fake identifiers (same shape as real, unmistakably synthetic) ────────

def ban() -> str:
    return f"9{random.randint(10**7, 10**8 - 1)}"           # 9 digits, 9-prefixed


def order_id() -> str:
    return f"{random.choice(STATES)}9{random.randint(10**8, 10**9 - 1)}"


def omtask() -> str:
    return f"OMTASK9{random.randint(10**7, 10**8 - 1)}"


def tn() -> str:
    return f"({random.randint(200, 989)}) 555-{random.randint(1000, 9999)}"


def case_id() -> str:
    return f"CS9{random.randint(10**6, 10**7 - 1)}"


def addr() -> str:
    return f"{random.randint(100, 9899)} {random.choice(['Elm','Cedar','Ridge','Manor','Kestrel','Alder'])} " \
           f"{random.choice(['St','Ave','Rd','Ln'])}, {random.choice(CITIES)}"


# ── families ────────────────────────────────────────────────────────────
# Each: subcategory (EMBEDDED), the RCA tag agents write, category, and templates.
# `steps` are functions of the context dict so identifiers stay consistent per row.

def _ctx() -> dict:
    lo, hi = random.choice(SPEEDS)
    return {"ban": ban(), "order": order_id(), "task": omtask(), "tn": tn(),
            "name": random.choice(NAMES), "case": case_id(), "addr": addr(),
            "lo": lo, "hi": hi, "banner_from": "postpaid fiber", "banner_to": "copper"}


FAMILIES = [
    dict(
        n=100, sub="Staging Stuck", tag="Order Completion - Staging Stuck", cat=CAT_SALES,
        sd=["An order was placed to upgrade the cx from {lo}-{hi} however the order is not reflecting on the account - {ban}",
            "Order for speed upgrade from {lo} to {hi} has not been reflecting / order number {order}",
            "Order placed but not showing in order history on BOSS - {ban}",
            "New order {order} not reflecting on customer account - {ban}",
            "Order for VAS addition is not reflecting on the account - {ban}"],
        desc=["An order was placed to upgrade the cx from {lo} to {hi} however the order is not reflecting on the account, please assist in having this updated. Order number {order} {name} {ban}",
              "Agent processed a speed upgrade from {lo} to {hi} and VAS but it has not reflected. Order number {order}. The order was accepted but the account still shows the old speed.",
              "{name} {ban} - the agent processed the order and it gives order number {order} but it is not showing on the order history in BOSS. Please assist.",
              "Order {order} was submitted two days ago and is still not visible on the account. Customer is calling in about the change not taking effect."],
        steps=[lambda c: [f"Located order {c['order']} on BAN {c['ban']} in OMS; order sat at stage Staging with no downstream task created.",
                          "Confirmed no validation error was blocking the order in the order table.",
                          f"Re-triggered the order push from the OM console for {c['order']}.",
                          f"Verified the order now reflects on BAN {c['ban']} and the account shows the {c['hi']} speed."],
               lambda c: [f"Checked order {c['order']} - present in OMS but not propagated to the account.",
                          "Cleared the stale staging record and resubmitted the order.",
                          f"Order {c['order']} completed and is now visible in the order history on BAN {c['ban']}.",
                          "Advised the requesting agent that no re-order is needed."]],
    ),
    dict(
        n=70, sub="Account Status Mismatch", tag="System Sync/C360 - Account Status Issue", cat=CAT_SALES,
        sd=["No Disconnect Order but Account is Inactive - {ban}",
            "Moving account is not showing to be active - {ban}",
            "Account shows inactive but customer still has service - {ban}",
            "Account is stuck in pending status and we cannot place an order - {ban}",
            "Unable to close OWS case as account status is wrong - {ban}"],
        desc=["We are unable to close the OWS case {case} due to BAN {ban} having no disconnection order but already being Inactive. We also have no option to cancel the service, and per the notes the account was supposed to remain in service.",
              "Customer is moving to a new address but the new account is not showing as active or as having a pending order. We cannot create a new account either. Please assist. {name} {ban}",
              "{name} at {addr} - account {ban} reads inactive in C360 but the customer confirms service is working. Billing is still being generated.",
              "The disconnect order on this account was cancelled but the account is still sitting in pending, so no new order can be raised for {name}."],
        steps=[lambda c: [f"Reviewed BAN {c['ban']} in C360 - status read Inactive with no disconnect order on file.",
                          "Cross-checked the order table; no cancellation or disconnect had been submitted.",
                          "Corrected the account status to Active to match the provisioned service.",
                          f"Confirmed OWS case {c['case']} can now be actioned and billing is aligned."],
               lambda c: [f"Confirmed the account {c['ban']} was left in Pending after order {c['order']} was cancelled.",
                          "Synced the account status from the order outcome.",
                          "Account status now reads correctly and a new order can be placed.",
                          "Screenshot attached for the requesting agent."]],
    ),
    dict(
        n=32, sub="Cancellation Completion", tag="Order Cancellation", cat=CAT_SALES,
        sd=["Disconnect order needs to be completed - {ban}",
            "Please cancel the value added service, already cancelled today - {ban}",
            "Unable to place disconnect as an order is already pending - {ban}",
            "Order {order} needs to be cancelled - {ban}"],
        desc=["Needing assistance making sure order {order} gets cancelled. We are trying to place a disconnect order for the same account but the system is not letting us do so. {name} {ban}",
              "Customer {name} called to cancel. There is already a disconnect order on the account and no service on it. Please assist with the disconnection. BAN {ban}",
              "Order {order} on BAN {ban} is pending and blocking any further change. Customer has already been told the service is cancelled."],
        steps=[lambda c: [f"Identified pending order {c['order']} on BAN {c['ban']} blocking the disconnect.",
                          f"Cancelled order {c['order']} in OMS.",
                          "Verified the disconnect option is now available on the account.",
                          "Account status moved to inactive as expected once the disconnect completed."]],
    ),
    dict(
        n=24, sub="Network Type Mismatch", tag="System Sync/C360 - Network Type Issue", cat=CAT_SALES,
        sd=["Wrong account banner - {ban}",
            "Account is under postpaid fiber but should be on copper banner - {ban}",
            "Banner does not match the provisioned network type - {ban}"],
        desc=["Asking for your assistance on correcting the banner from postpaid fiber to copper. BAN {ban}",
              "Customer account is under post paid fiber, it needs to be on the copper banner, and the customer wants to add telephone again on their account. This account should be turned back to copper so we can migrate it again. {ban}",
              "The account shows the wrong banner in C360 and the agent cannot proceed with the order for {name}. BAN {ban}"],
        steps=[lambda c: [f"Compared the banner on BAN {c['ban']} against the provisioned network type - banner read Postpaid Fiber, provisioned network was Copper.",
                          "Updated the banner to match what is actually provisioned.",
                          "Confirmed the account now shows the copper banner in C360.",
                          "Agent can now add the telephone service; screenshot attached."]],
    ),
    dict(
        n=22, sub="Service Profile Issue", tag="System Sync/C360 - Service Profile Issue", cat=CAT_SALES,
        sd=["Service profile is not showing the correct services - {ban}",
            "Wrong account banner on service profile - {ban}",
            "Internet shows in change plan but not on the service profile - {ban}"],
        desc=["Asking your assistance on changing the banner from postpaid fiber to copper on the service profile. BAN {ban}",
              "Order completed some time ago however the system is still billing the customer. The service profile shows only phone, and change plan does not allow any changes as internally the system does not recognise the change. Please correct the account. {ban}",
              "Service profile for {name} on BAN {ban} is out of step with what was ordered - please align it."],
        steps=[lambda c: [f"Pulled the service profile for BAN {c['ban']} and compared it with the completed orders.",
                          "Found the profile had not been refreshed after the last change order.",
                          "Re-synced the service profile from the order history.",
                          "Confirmed the profile and change plan now agree; screenshot attached."]],
    ),
    dict(
        n=16, sub="Order status sync issue", tag="System Sync/C360 - Order Status Sync Issue", cat=CAT_SALES,
        sd=["Order status not syncing between OMS and C360 - {order}",
            "Order shows complete in OMS but pending on the account - {ban}"],
        desc=["Order {order} on BAN {ban} shows as completed in OMS but the account still shows it pending. Agent cannot raise the next order for {name}.",
              "The order status is out of sync between systems for BAN {ban}. Please align order {order}."],
        steps=[lambda c: [f"Compared order {c['order']} status in OMS against the account view - OMS said Completed, account said Pending.",
                          "Re-published the order status event to C360.",
                          f"Verified BAN {c['ban']} now shows the order as completed.",
                          "Confirmed the follow-on order can be placed."]],
    ),
    dict(
        n=14, sub="Task Closure", tag="Order Completion - Task Closure", cat=CAT_SALES,
        sd=["Order stuck with an open task past due date - {order}",
            "Fallout task needs to be closed on order {order}"],
        desc=["The install was completed on site but order {order} has not moved to complete. It is sitting past its due date and the customer is not being billed correctly. BAN {ban}",
              "Task {task} on order {order} is blocking completion. Tech has confirmed the job is done. Please close the task. BAN {ban}"],
        steps=[lambda c: [f"Reviewed order {c['order']} on BAN {c['ban']} - held at Order Completion by open task {c['task']}.",
                          "Confirmed with the field record that the job was already completed.",
                          f"Closed task {c['task']} and allowed the order to progress.",
                          "Order completed and billing is now correct."]],
    ),
    dict(
        n=12, sub="Account Type Mismatch", tag="System Sync/C360 - Account Type Issue", cat=CAT_SALES,
        sd=["Wrong account type on the account - {ban}",
            "Please remove the FIBER AVAILABLE toggle, account should be FIBER POSTPAID - {ban}"],
        desc=["Asking for your assistance on correcting the account type as the account already has fiber service. BAN {ban}",
              "Please remove the FIBER AVAILABLE toggle on the account. The account should be FIBER POSTPAID already. BAN {ban}"],
        steps=[lambda c: [f"Checked the account type flags on BAN {c['ban']} against the provisioned product.",
                          "Removed the incorrect availability toggle.",
                          "Account type updated to the correct postpaid value.",
                          "Screenshot attached for reference."]],
    ),
    dict(
        n=10, sub="Pending Order Error in change plan", tag="Change Plan - Pending Order Error", cat=CAT_SALES,
        sd=["Unable to make changes, shows a pending order and there is nothing on record - {ban}",
            "PENDING ORDER ISSUE - CANNOT MAKE CHANGES - {ban}"],
        desc=["Asking for your assistance to release account {ban}, unable to make changes. There are no pending orders or fallout tasks visible on the account for {name}.",
              "Can you please help with account {ban}? Change plan throws a pending order error but nothing is showing on the record."],
        steps=[lambda c: [f"Investigated the pending order flag on BAN {c['ban']} - a stale order lock remained after order {c['order']} closed.",
                          "Cleared the orphaned pending order reference.",
                          "Verified change plan is now available on the account.",
                          "Screenshot attached; advised the agent to retry the change."]],
    ),
    dict(
        n=10, sub="Email / Phone Details Update", tag="Customer Details Update - Email / Phone Update", cat=CAT_CONTACT,
        sd=["Please add alternate phone number - {ban}",
            "Unable to update the email address on the account - {ban}"],
        desc=["{name} {ban} - please assist in adding alternate phone number {tn}. We are unable to add it manually under the communication profile.",
              "Customer {name} wants the email on BAN {ban} corrected; the communication profile will not save the change."],
        steps=[lambda c: [f"Opened the communication profile for BAN {c['ban']} in BOSS AD.",
                          "Confirmed the field was editable once the profile was refreshed.",
                          f"Added the alternate contact {c['tn']} and saved.",
                          "Screenshot attached for the requesting agent."]],
    ),
    dict(
        n=18, sub="Digital Buyflow", tag="Fallout to IT", cat=CAT_SALES,
        sd=["Remove TN {tn} on BAN {ban} and replace with a new number",
            "Buy Flow order capture failed for BAN {ban}",
            "Change plan completed but changes are not effective - {ban}"],
        desc=["DUE TO A BUY FLOW DEFECT STILL BEING PROCESSED WITH THE BUYFLOW TEAM PLEASE REMOVE TN {tn} ON BAN {ban} AND REPLACE IT. DO NOT CLOSE THIS WITHOUT WORKING. WE CANNOT ADD NUMBERS TO AN ACCOUNT WITHOUT DISCONNECTING A CUSTOMER WHO ALREADY HAS SERVICE.",
              "Order capture in Buy Flow produced the wrong service number for {name} on BAN {ban}. Please correct the service profile number."],
        steps=[lambda c: [f"Confirmed the defect on BAN {c['ban']} originated in Buy Flow order capture, not in BOSS OM.",
                          "Reassigned the incident to the Buy Flow team who own the capture defect.",
                          "No change made to the account by BOSS OM; identifiers left as reported.",
                          "Incident left open for the receiving team."]],
    ),
    dict(
        n=34, sub="Other", tag="", cat=CAT_CONTACT,
        sd=["Order issue - {ban}", "Assistance needed on account - {ban}",
            "Unable to proceed with order for customer - {ban}",
            "Billing does not match the order placed - {ban}"],
        desc=["Customer {name} on BAN {ban} is reporting an issue with the recent order {order}. Please review and advise.",
              "Requesting assistance on account {ban}. The agent is unable to proceed and the customer is waiting on a callback.",
              "{name} at {addr} reports the bill does not match what was ordered on {order}. Please check BAN {ban}."],
        steps=[lambda c: [f"Reviewed BAN {c['ban']} and order {c['order']} end to end.",
                          "No system defect found; the account reflects what was ordered.",
                          "Explained the expected behaviour to the requesting agent.",
                          "No change required on the account."]],
    ),
]

BOILERPLATE = [
    "Assignment Rule 'BOSS OM Fallout Routing' has been applied to this task.",
    "Record Producer: BOSS OM Support Request was used to create this incident.",
    "This incident has been automatically closed after 3 days in Resolved state.",
    "Priority has been recalculated based on impact and urgency.",
]

AGENT_NOTES = [
    "Assigning to BOSS OM for review, please check the account and advise.",
    "Escalating as the customer has called back twice on this.",
    "Checked the account and the issue is reproducible; handing to the platform team.",
    "Requesting an update, the customer is waiting on this.",
    "Attaching screenshots of what the agent is seeing on the account.",
]


def build_rows() -> list:
    rows, n = [], 0
    for fam in FAMILIES:
        for _ in range(fam["n"]):
            n += 1
            c = _ctx()
            sd = random.choice(fam["sd"]).format(**c)
            desc = random.choice(fam["desc"]).format(**c)
            steps = random.choice(fam["steps"])(c)

            close = " ".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            if fam["tag"]:
                close += f" {{RCA TAG : {fam['tag']}}}"
            close += f" BAN: {c['ban']}"
            if "{order}" in random.choice(fam["desc"]) or "order" in desc.lower():
                close += f" Order ID: {c['order']}"

            notes = [random.choice(BOILERPLATE), random.choice(AGENT_NOTES)]
            if random.random() < 0.4:
                notes.append(random.choice(BOILERPLATE))
            random.shuffle(notes)

            rows.append({
                "number": f"INC90{n:05d}",
                "assigned_to": random.choice(AGENTS),
                "state": "Closed",
                "comments_and_work_notes": "\n".join(notes),
                "category": fam["cat"],
                "assignment_group": GROUP,
                "u_issue_type": fam["sub"],
                "short_description": sd,
                "description": desc,
                "close_notes": close,
            })
    random.shuffle(rows)
    # Renumber after shuffling so numbers do not encode the family.
    for i, r in enumerate(rows, 1):
        r["number"] = f"INC90{i:05d}"
    return rows


def write_kb():
    rows = build_rows()
    pd.DataFrame(rows).to_excel(KB_PATH, index=False)
    print(f"wrote {len(rows)} synthetic closed tickets -> {KB_PATH}")
    import collections
    for sub, k in collections.Counter(r["u_issue_type"] for r in rows).most_common():
        print(f"   {k:>4}  {sub}")
    return rows


if __name__ == "__main__":
    write_kb()
    if "--queue" in sys.argv:
        print("\n(queue generation is in make_synthetic_queue.py)")
