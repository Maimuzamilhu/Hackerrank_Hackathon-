"""Stage 2 verification: request intent (Step 3), identity resolution (Step 4), audit_at cutoff (Step 5).

Checks against the sample answers where they exist, against hand-written edge cases for
intent, and against independent reconstructions for identity and time filtering.
"""
import json
from collections import Counter

from ghost.case import build_cases
from ghost.data import load_dataset
from ghost.evidence import load_evidence
from ghost.identity import IdentityIndex, email_hash, norm_email
from ghost.intent import classify_request

# Our own intent cases: (text, authorizes_deletion). Written to break naive keyword rules.
CUSTOM_INTENT_CASES = [
    ("Please delete my account and everything linked to it.", True),
    ("Close my account for good and wipe my personal data.", True),
    ("I want my profile erased, including old support tickets.", True),
    ("Bitte löschen Sie mein Konto und alle meine Daten.", True),
    ("Vorrei la cancellazione definitiva del mio account.", True),
    ("请删除我的账户和所有个人信息。", True),
    ("Quiero borrar mi cuenta y todos mis datos personales.", True),
    ("Merci de supprimer mon compte.", True),
    ("Please remove me from marketing lists only. I still want order emails.", False),
    ("Please do not delete my account, I only need a copy of my data.", False),
    ("Can you correct my address without removing my order history?", False),
    ("Unsubscribe me from newsletters. Keep the account open; I still use it.", False),
    ("I need my invoices from 2025 for tax purposes.", False),
    ("Please update my phone number to the new one.", False),
    ("Send me an export of my data. This is not a deletion request.", False),
    ("No quiero eliminar mi cuenta, solo cambiar el correo.", False),
    ("Ne supprimez pas mon compte, je veux juste corriger mon nom.", False),
    ("アカウントは削除しないでください。メールアドレスを変更したいです。", False),
    ("계정은 삭제하지 마세요. 비밀번호만 재설정해 주세요.", False),
    ("Can you verify that my account was deleted? Please do not treat the other record as mine.", True),
    ("Please confirm my profile is gone and that nothing left can identify me.", True),
    ("Make sure the analytics data is anonymized so it no longer points to me.", True),
    ("Is my order gone? It was supposed to arrive yesterday.", False),
    ("Please check whether my account is still active; I want to keep using it.", False),
]


class Report:
    def __init__(self):
        self.failures, self.passes = [], 0

    def check(self, ok, msg):
        if ok:
            self.passes += 1
        else:
            self.failures.append(msg)


def run() -> int:
    r = Report()
    ds = load_dataset()
    evidence = load_evidence(ds)

    print("[Step 3] Request intent")
    sample_status = {row["case_id"]: row["deletion_status"] for row in ds.sample_output}
    for req in ds.sample_requests:
        intent = classify_request(req["request_text"])
        expected = sample_status[req["case_id"]] != "NOT_APPLICABLE"
        r.check(intent.authorizes_deletion == expected, f"sample {req['case_id']}: intent {intent} but answer is {sample_status[req['case_id']]}")
    print(f"  samples: {sum(1 for q in ds.sample_requests if classify_request(q['request_text']).authorizes_deletion == (sample_status[q['case_id']] != 'NOT_APPLICABLE'))}/{len(ds.sample_requests)} agree with the sample answers")

    custom_ok = 0
    for text, expected in CUSTOM_INTENT_CASES:
        got = classify_request(text)
        ok = got.authorizes_deletion == expected
        custom_ok += ok
        r.check(ok, f"custom intent case: {text!r} -> {got}, expected {expected}")
    print(f"  custom edge cases: {custom_ok}/{len(CUSTOM_INTENT_CASES)}")

    intents = {q["case_id"]: classify_request(q["request_text"]) for q in ds.requests}
    print("  evaluation set:", dict(Counter("DELETION" if i.authorizes_deletion else "NOT_APPLICABLE" for i in intents.values())))
    print("  deciding cue (deletion):", dict(Counter(i.cue.lower() for i in intents.values() if i.authorizes_deletion).most_common()))
    for q in ds.requests:
        i = intents[q["case_id"]]
        if not i.authorizes_deletion:
            print(f"    NOT_APPLICABLE {q['case_id']}  cue={i.cue!r}  | {q['request_text'][:90]}")

    # Independent corroboration: privacy intake logged 10 non-deletion classifications by
    # account id. Link those accounts to users through the evidence and compare.
    index = IdentityIndex(ds)
    intake_accounts = {a["resource_id"] for a in ds.activity if a["event_type"] == "CLASSIFY_REQUEST"}
    account_users = {}
    for obs in evidence.values():
        for o in obs:
            if o.resource_id in intake_accounts:
                for tok in o.identifiers():
                    if "@" in tok and (owners := index.by_email.get(tok)) and len(owners) == 1:
                        account_users.setdefault(o.resource_id, set()).update(owners)
                    elif tok.startswith("user_") and tok in ds.identities:
                        account_users.setdefault(o.resource_id, set()).add(tok)
    na_users = {index.resolve(q["email"], q["audit_at"]).user_id for q in ds.requests if not intents[q["case_id"]].authorizes_deletion}
    intake_users = set().union(*account_users.values()) if account_users else set()
    print(f"  intake log: {len(intake_accounts)} accounts classified non-deletion, linked to {len(intake_users)} users; "
          f"overlap with our NOT_APPLICABLE users: {len(na_users & intake_users)}/{len(na_users)}")

    print("\n[Step 4] Identity resolution")
    cases = build_cases(ds, evidence, ds.requests + ds.sample_requests)
    unresolved = [c.case_id for c in cases if c.intent.authorizes_deletion and c.identity is None]
    r.check(not unresolved, f"unresolved identities: {unresolved}")
    resolved = [c for c in cases if c.identity]
    print(f"  resolved {len(resolved)}/{sum(1 for c in cases if c.intent.authorizes_deletion)} deletion cases;",
          dict(Counter(c.identity.matched_via for c in resolved)))
    for c in resolved:
        ident, req = c.identity, c.request
        r.check(norm_email(req["email"]) in ident.emails, f"{c.case_id}: request email not in identity")
        r.check(all(email_hash(e) in ident.email_hashes for e in ident.emails), f"{c.case_id}: missing hash for an email")
        cur = ds.identities[ident.user_id]
        r.check(norm_email(cur["email"]) in ident.emails, f"{c.case_id}: current email missing")
        hist = [a for a in ds.audit_log if a["user_id"] == ident.user_id and a["event_time"] <= req["audit_at"]]
        r.check(sorted(a["audit_id"] for a in hist) == sorted(ident.audit_ids), f"{c.case_id}: audit history incomplete")
        for a in hist:
            if a["field_name"] == "email":
                r.check(norm_email(a["old_value"]) in ident.emails, f"{c.case_id}: old email {a['audit_id']} missing")
    multi = Counter(len(c.identity.emails) for c in resolved)
    print("  emails per identity:", dict(sorted(multi.items())))

    # Do the sample answers' resources connect to the resolved identity through some evidence?
    # (Informational for Step 6: shows how much the identifiers alone already find.)
    linked = total = 0
    for c in resolved:
        if c.case_id not in sample_status:
            continue
        toks = c.identity.tokens()
        for step in json.loads(next(x["steps_json"] for x in ds.sample_output if x["case_id"] == c.case_id)):
            total += 1
            rid = step["resource_id"]
            hit = any(o.resource_id == rid and (o.identifiers() & toks) for obs in evidence.values() for o in obs)
            hit = hit or any(a["resource_id"] == rid and any(t in a["details"].lower() for t in toks) for a in ds.activity)
            linked += hit
    print(f"  sample answer steps whose resource is tied to the identity by direct identifiers: {linked}/{total}")

    print("\n[Step 5] audit_at cutoff")
    dropped_files = dropped_rows = 0
    for c in resolved:
        audit_at = c.request["audit_at"]
        r.check(all(a["event_time"] <= audit_at for a in c.activity), f"{c.case_id}: late activity kept")
        r.check(all(o.available_at <= audit_at for obs in c.evidence.values() for o in obs), f"{c.case_id}: late evidence kept")
        expected_files = {f for f, m in ds.manifest.items() if m["captured_at"] <= audit_at}
        r.check(set(c.evidence) <= expected_files, f"{c.case_id}: kept a file captured after audit_at")
        r.check(len(c.activity) == sum(1 for a in ds.activity if a["event_time"] <= audit_at), f"{c.case_id}: dropped an on-time activity")
        dropped_files += len(ds.manifest) - len(c.evidence)
        dropped_rows += sum(len(evidence[f]) for f in c.evidence) - sum(len(v) for v in c.evidence.values())
    print(f"  avg files outside the window per case: {dropped_files / max(len(resolved), 1):.1f}; "
          f"rows dropped inside kept files (dated after audit_at): {dropped_rows}")

    # Every evidence id cited by the sample answers must survive our cutoff for that case.
    cited = kept = 0
    for c in resolved:
        if c.case_id not in sample_status:
            continue
        for step in json.loads(next(x["steps_json"] for x in ds.sample_output if x["case_id"] == c.case_id)):
            eid = step["evidence_id"]
            if not eid:
                continue
            cited += 1
            ok = eid in c.evidence or eid in {a["activity_id"] for a in c.activity} or eid in c.identity.audit_ids
            kept += ok
            r.check(ok, f"{c.case_id}: cited {eid} is outside our audit_at window")
    print(f"  sample citations inside our window: {kept}/{cited}")

    print(f"\nPASSED {r.passes} checks, {len(r.failures)} failures")
    for f in r.failures[:40]:
        print("  FAIL", f)
    return 1 if r.failures else 0
