# Evaluation Report: Ghost Data Still Exists

All numbers below come from `python code/evaluation/main.py`. That run builds every case from
`dataset/` only, scores it against the 32 public sample answers, runs our 8 custom cases, and
summarises the full 518-request run that wrote `output.csv`.

> **Read the sample numbers as training-set numbers.** The 32 sample answers were used while
> building the system: to understand the format and to tune rules (deletion-worker timing
> windows, intent cues, evidence semantics). They are not a held-out test. To guard against
> over-fitting, we also ran integrity checks on all 550 cases (section 5) and wrote custom
> cases whose answers we derived by hand (section 4).

## 1. Case-level deletion-status accuracy

| Metric | Result |
| --- | --- |
| Status accuracy (32 samples) | **32/32 (100.0%)** |
| Row exact match (status + every step, in order) | 26/32 (81.2%) |

Status confusion matrix (gold → ours):

| gold \ ours | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
| --- | --- | --- | --- |
| COMPLETE | 10 | 0 | 0 |
| INCOMPLETE | 0 | 22 | 0 |

No sample is NOT_APPLICABLE, so intent was also tested on 24 hand-written edge cases (all
pass; `python code/main.py verify-stage2`) and on the custom cases in section 4.

## 2. Step-level and field-level accuracy

A step matches when all six content fields (`phase, system_id, resource_id, operation, state,
evidence_id`) are equal.

| Steps | Gold | Predicted | Matched | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| FINDING | 41 | 41 | 41 | 1.000 | 1.000 |
| REMEDIATION | 41 | 41 | 41 | 1.000 | 1.000 |
| VERIFICATION | 111 | 108 | 105 | 0.972 | 0.946 |
| **All** | **193** | **190** | **187** | **0.984** | **0.969** (F1 0.977) |

Field-level accuracy: each gold step is aligned to our step with the same phase, resource and
operation. An unaligned gold step scores 0 on every field.

| Field | Accuracy |
| --- | --- |
| step_no | 182/193 (94.3%) |
| phase | 187/193 (96.9%) |
| system_id | 187/193 (96.9%) |
| resource_id | 187/193 (96.9%) |
| operation | 187/193 (96.9%) |
| state | 187/193 (96.9%) |
| evidence_id | 187/193 (96.9%) |
| **All fields** | **1304/1351 (96.5%)** |

Every finding, its evidence citation and its remediation is correct (41/41 each). All errors
are in the verification lists, where one resource is missing or the wrong one was picked
(details in section 6).

## 3. Sequence accuracy

| Metric | Result |
| --- | --- |
| Exact step sequence | 26/32 (81.2%) |
| Mean longest-common-subsequence / gold length | 0.960 |
| Our sort applied to each gold step set reproduces the gold order | 32/32 |

The order follows the stated rule: phase (FINDING → REMEDIATION → VERIFICATION), then the
Allowed Operations table order, then `system_id`, then `resource_id`. So flows are disabled
and restores blocked before any record is changed. The 6 inexact sequences come from the 6
resource-set misses in section 6, not from ordering.

## 4. Custom evaluation cases (created by us)

Each custom case rewrites one existing request, keeping the same `case_id` so the rest of the
data world is unchanged. We wrote the expected answer by hand from the raw files or the
sample answers, before checking the program. Code: `CUSTOM_CASES` in `code/evaluation/main.py`.

| # | Case | Change | Why the answer is what it is | Expected | Result |
| --- | --- | --- | --- | --- | --- |
| C1 | Historical email | case_29 resubmitted with the person's *other* email (`noah.f4380@outlook.com`, from `user_audit_log.csv`) | Same person, same request. The answer must not depend on which email is supplied. | INCOMPLETE, all 14 steps = case_29 sample answer | PASS |
| C2 | German deletion request | case_13 text replaced by *"Bitte löschen Sie mein Kundenkonto und alle meine personenbezogenen Daten dauerhaft."* | Only the language changes. | INCOMPLETE, all 8 steps = case_13 sample answer | PASS |
| C3 | Export request, not deletion | case_13 text: a German request for a copy of the data that keeps the account | Access/export is not a deletion. | NOT_APPLICABLE, `[]` | PASS |
| C4 | Opt-out that says "delete" | case_29 text: *"…stop sending me marketing emails and delete me from the newsletter list, but do not delete my account."* | Marketing opt-out with an explicit keep. | NOT_APPLICABLE, `[]` | PASS |
| C5 | Time travel | case_26 `audit_at` moved to 2026-09-15T11:00Z, before evidence_92 was captured (11:44:55Z) | Evidence captured after `audit_at` must not be used. | evidence_92 never cited; every citation captured ≤ new `audit_at` | PASS |
| C6 | Cache miss is not a deletion | case_267 as given | evidence_25 line 41 reads audit_record_6621 with `rows="1"`. The same trace's `cache_lookup result="miss"` is about the cache. | `VERIFY_RECORD_PRESENT audit_record_6621` citing evidence_25 | PASS |
| C7 | Restorable backup on a screen | case_55 as given | Screenshot evidence_32 lists backup_9287 with the person's email, checksum verified, no restore block. The request was never started. | INCOMPLETE; `REPORT_RESTORE_RISK backup_9287` citing evidence_32 → `BLOCK_RESTORE` → `VERIFY_RESTORE_BLOCKED` | PASS |
| C8 | Keep cue beats delete cue | case_13 text: *"…Please do NOT delete my account, I still use it."* | Mentions deletion only to refuse it. | NOT_APPLICABLE, `[]` | PASS |

**8/8 pass.** C6 and C7 are regression tests for two bugs found while building the output
(section 6).

## 5. Checks on all 550 cases (no gold needed)

| Check | Command | Result |
| --- | --- | --- |
| Raw files vs parsed evidence (every row, line, record, page, screenshot) | `python code/main.py verify` | 7,228 checks, 0 failures |
| Intent, identity resolution, `audit_at` cutoff | `python code/main.py verify-stage2` | 4,559 checks, 0 failures |
| Evidence rules, status/finding consistency, one owner per resource, over-fitting | `python code/main.py validate-stage3` | 11,965 checks, 0 failures |
| Output contract (header, 518 rows, order, numbering, operation↔state, evidence rules) | `python code/main.py verify-stage4` | 0 errors |

What these show:
- No citation is captured after its case's `audit_at`.
- Every cited file actually refers to the cited resource.
- No resource is assigned to two different people.
- No finding rests on a guessed link.
- Deletion-worker timing windows (tuned on samples) fit 1,459 of the 1,466 worker events across all requests. Only 1 event sits within 3 minutes of a window edge, so the windows are not fitted to the samples.

**Full run:** 518 rows: 355 INCOMPLETE, 153 COMPLETE, 10 NOT_APPLICABLE (case_533–542; this
matches the 10 non-deletion classifications in the activity stream). 3,271 steps. The run is
deterministic: two runs give byte-identical `output.csv`.

The workflow's claims compared with what the evidence shows:

| reported_status | audited COMPLETE | audited INCOMPLETE | NOT_APPLICABLE |
| --- | --- | --- | --- |
| COMPLETE | 91 | **163** | 0 |
| PARTIALLY_COMPLETE | 36 | 116 | 0 |
| NOT_STARTED | 26 | 76 | 10 |

163 of 254 cases the workflow called COMPLETE still have ghost data.

## 6. Failures found and improvements made

### Remaining sample misses (6 cases, all in verification lists)

| Case | Miss | Cause |
| --- | --- | --- |
| case_04 | `account_004` not verified | The id appears nowhere in participant data (no evidence, no activity). |
| case_18, case_20 | `audit_record_6645` / `audit_record_5569` not verified | Same: these ids exist only in the sample answers. |
| case_21, case_22, case_23 | Wrong primary account picked | Several requests share one deletion-worker time slot, and the only link from account to person is that timing. We guess by constraint propagation; 8 of 11 such guesses on samples are right. |

We did not hard-code any of these. Using the answers would break the participant-data-only rule.

### Bugs found and fixed during development

| Stage | Bug | How it was found | Fix |
| --- | --- | --- | --- |
| 1 Parse | Literal `null` in logs read as a value | raw-vs-parsed verifier | Parse `null` as missing; a later null never erases an earlier value |
| 1 Parse | Nested JSON resources dropped | verifier | Walk nested records that carry their own resource_id |
| 1 Parse | Topology SQL files and an empty SQL had no capture scope, so absence could not be proven | verifier | Emit scope records from SQL headers |
| 1 Parse | Screenshot checksum line read as an email hash | identifier audit | Strip header lines; skip `_`-prefixed keys |
| 2 Intent | case_26 ("confirm the profile is gone", no delete verb) missed | sample status | Follow-up/anonymize cues + tests |
| 3 State | SQL rows timed by `updated_at` instead of capture time | sample evidence | Rows are observed at the snapshot capture time |
| 3 State | A bundle missing a record was read as global absence | sample status | Absence counts only from full-table snapshots, or from bundles that cover the person |
| 3 State | Admin-console screenshots ignored; mixed `customer_data_*` replicas treated as full tables; activity feeds on screens created fake ownership | `validate-stage3` on all cases (the samples were already 32/32) | Screens count for the record in their header/panel; only single-system topology dumps are full tables; feeds don't link |
| 3 Link | Earlier of two requests by one person took the later request's resources | validator | Scope each request by the previous and next `audit_at` |
| 3 Link | Screens show business names, not ids (`delivery_confirmation_19.pdf`) | validator | Catalog aliases from `LINK_RESOURCE_IDENTITY` events |
| 4 Output | Every log `cache_lookup … result=miss` made its whole trace read "not found" (93 traces), so present audit records looked deleted | a COMPLETE case had nothing to cite | Store cache results separately (custom case C6) |
| 4 Output | Backup listed with the person's email in the live recovery console was not a restore risk, so case_55 was COMPLETE with no proof | contract check | A recovery-console row with exposed identifiers and no stated block is an open restore path (C7) |

### Known limitations

- **No step without proof.** 13 resources in COMPLETE cases (9 timing-guessed accounts, 4 search entries) have no participant evidence of their state. They get no verification step, because a COMPLETE step must cite evidence that proves the state.
- **12 requests have no linked primary account** in the evidence.
- **Unknown email.** A request email that matches nobody is not supported (all 540 real requests resolve).
- **Late evidence for ownership.** Files captured after `audit_at` may be used to decide *who owns* a resource. They are never used for its *state* or cited. This is our reading of the time rule.
