"""
Discover which incident fields a ServiceNow instance actually requires
----------------------------------------------------------------------
The required-field list for ticket creation must come from the instance rather
than from assumption, so this reads it. Entirely READ ONLY.

Two sources matter, and only checking the first is a common mistake:

  * sys_dictionary, where `mandatory=true` marks a field required at the data
    dictionary level, on `incident` and on its parent table `task`.
  * sys_data_policy2 and its rules. Data policies, unlike UI policies, ARE enforced
    on REST inserts, so a policy can make a field mandatory even when the dictionary
    says it is optional. A script that checks only the dictionary will be surprised
    by a rejected insert.

Usage (from backend/):
    ../rca/bin/python discover_required_fields.py
"""

import sys

from dotenv import load_dotenv
load_dotenv()

import servicenow_client as sn


def _rows(label, table, params):
    try:
        out = sn._table_get(table, params)
        print(f"[ok]   {label}: {len(out)} row(s)")
        return out
    except Exception as e:
        print(f"[FAIL] {label}: {type(e).__name__} {str(e)[:200]}")
        return None


def main():
    print(f"instance: {sn.INSTANCE}")
    print(f"auth:     {'OAuth' if sn.CLIENT_ID and sn.CLIENT_SECRET else 'Basic'}")
    print()

    if _rows("read the incident table", "incident",
             {"sysparm_limit": 1, "sysparm_fields": "number"}) is None:
        print("\nCannot read the instance, so nothing below can be trusted. Check "
              "SERVICENOW_* in backend/.env first.")
        return 1

    mandatory = []
    for table in ("incident", "task"):
        rows = _rows(f"dictionary-mandatory fields on '{table}'", "sys_dictionary",
                     {"sysparm_query": f"name={table}^mandatory=true",
                      "sysparm_fields": "element,column_label,internal_type,default_value",
                      "sysparm_limit": 200}) or []
        for r in rows:
            el = (r.get("element") or "").strip()
            if not el:
                continue
            mandatory.append((el, r.get("column_label", ""), r.get("default_value", "")))
            print(f"         {el:<26} {r.get('column_label','')!r} "
                  f"default={r.get('default_value','')!r}")

    print()
    policies = _rows("active data policies on incident", "sys_data_policy2",
                     {"sysparm_query": "model_table=incident^active=true",
                      "sysparm_fields": "sys_id,short_description,apply_soap,apply_import_set",
                      "sysparm_limit": 50}) or []
    for p in policies:
        print(f"         {p.get('short_description','')!r} "
              f"applies to REST/SOAP={p.get('apply_soap')}")
    ids = ",".join(p["sys_id"] for p in policies if p.get("sys_id"))
    if ids:
        rules = _rows("data policy rules", "sys_data_policy_rule",
                      {"sysparm_query": f"sys_data_policyIN{ids}^mandatory=true",
                       "sysparm_fields": "field,mandatory,disabled", "sysparm_limit": 200}) or []
        for r in rules:
            print(f"         policy-mandatory: {r.get('field')} (disabled={r.get('disabled')})")
            mandatory.append((r.get("field", ""), "via data policy", ""))

    print()
    print("=" * 70)
    required_no_default = sorted({el for el, _, dflt in mandatory if el and not dflt})
    print("Fields that are mandatory AND have no default, so a create must supply them:")
    for el in required_no_default or ["  (none beyond what the API defaults)"]:
        print(f"  - {el}")
    print()
    print("Compare this against REQUIRED_TICKET_FIELDS in fallout_engine.py and update "
          "that list. It is the single place the ticket flow reads its requirements from.")

    print()
    grp = _rows(f"group {sn.DEMO_GROUP!r}", "sys_user_group",
                {"sysparm_query": f"name={sn.DEMO_GROUP}", "sysparm_fields": "name,sys_id"})
    print(f"         {sn.DEMO_GROUP!r} exists: {'yes' if grp else 'NO, run seed_demo_tickets.py'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
