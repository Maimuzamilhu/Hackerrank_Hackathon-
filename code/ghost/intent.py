"""Step 3: does the request text authorize deletion?

Deterministic, multilingual, and explainable: every decision returns the cue that drove it.
Order of precedence:
  1. An explicit "keep / do not delete / not a deletion request / marketing only" statement
     means the request does NOT authorize deletion, even if a deletion word appears
     ("correct it without removing any of my records", "remove me from marketing lists only").
  2. Otherwise any deletion cue (delete, erase, remove, close account, deletion, and the
     equivalents in Spanish, Portuguese, French, German, Italian, Japanese, Korean, Chinese)
     authorizes deletion.
  3. No cue at all: access, export, correction, login or billing requests -> not deletion.
"""
import re
from dataclasses import dataclass

DELETION_CUES = [
    # English
    r"\bdelet(?:e|ed|es|ing|ion)\b", r"\beras(?:e|ed|es|ing|ure)\b", r"\bremov(?:e|ed|es|ing|al)\b",
    r"\bclos(?:e|ed|es|ing|ure)\b[^.?!]*\baccount\b", r"\baccount\b[^.?!]*\bclos(?:e|ed|ing|ure)\b",
    r"\bdeactivat(?:e|ed|ion)\b", r"\bforget me\b", r"\bwipe\b",
    # follow-ups asking us to confirm a deletion actually took effect
    r"\b(?:account|profile|data)\b[^.?!]*\b(?:is|was|be|been|are) (?:gone|no longer (?:there|available|visible))\b",
    r"\b(?:cannot|can't|can no longer|no longer) (?:identify|be linked to|point (?:back )?to) me\b",
    r"\b(?:anonymi[sz](?:e|ed|ation)|de-identif(?:y|ied|ication))\b",
    # Spanish / Portuguese / Italian
    r"\belimin(?:ar|e|en|ación|ação|are|azione)\b", r"\bborr(?:ar|en|ado)\b", r"\bexclu(?:ir|são|am)\b",
    r"\bapag(?:ar|uem)\b", r"\bcancell(?:are|azione)\b", r"\bsupresión\b",
    # French / German
    r"\bsuppr(?:ession|imer|imez)\b", r"\beffac(?:er|ez|ement)\b", r"\bl[öo]sch(?:en|ung)\b",
    # Japanese / Korean / Chinese
    r"削除", r"退会", r"삭제", r"탈퇴", r"删除", r"注销",
]

KEEP_CUES = [
    # explicit "this is not a deletion request"
    r"\bnot (?:asking|requesting|looking) (?:for )?(?:a |an |any )?(?:deletion|erasure|removal|to (?:delete|erase|remove|close))",
    r"\bnot a (?:deletion|erasure) request\b", r"\bnot a request to (?:delete|erase|remove|close)",
    r"\b(?:do not|don't|dont|never|please don't|please do not) (?:delete|erase|remove|close|deactivate)\b",
    r"\bwithout (?:deleting|removing|erasing|closing)\b",
    # explicit wish to keep the account
    r"\b(?:keep|leave) (?:the|my|this) (?:account|profile)\b", r"\bleave (?:the|my) account active\b",
    r"\b(?:continue|keep) using\b", r"\bstill (?:need|use|want) (?:it|the account|this account|my account)\b",
    # marketing-only opt-out is not account deletion
    r"\bmarketing\b[^.?!]*\bonly\b", r"\bonly\b[^.?!]*\bmarketing\b",
    # other languages
    r"\bno (?:quiero|deseo) (?:eliminar|borrar)\b", r"\bsin (?:eliminar|borrar)\b",
    r"\bnão (?:excluam|apaguem|quero excluir)\b", r"\bsem (?:excluir|apagar)\b",
    r"\bne (?:supprimez|supprimer) pas\b", r"\bsans supprimer\b", r"\bnicht l[öo]schen\b",
    r"削除しないで", r"削除は不要", r"삭제하지 마", r"삭제를 원하지 않", r"不要删除",
]

_DELETION = [re.compile(p, re.I) for p in DELETION_CUES]
_KEEP = [re.compile(p, re.I) for p in KEEP_CUES]


@dataclass(frozen=True)
class Intent:
    authorizes_deletion: bool
    reason: str          # "keep_statement" | "deletion_cue" | "no_deletion_cue"
    cue: str | None      # the exact text that decided it


def classify_request(text: str) -> Intent:
    for rx in _KEEP:
        if m := rx.search(text):
            return Intent(False, "keep_statement", m.group(0))
    for rx in _DELETION:
        if m := rx.search(text):
            return Intent(True, "deletion_cue", m.group(0))
    return Intent(False, "no_deletion_cue", None)
