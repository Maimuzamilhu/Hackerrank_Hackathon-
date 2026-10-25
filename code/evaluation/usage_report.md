# Usage Report: final full-dataset run

The run that produced `output.csv` was `python code/main.py run`. It processed all 518
evaluation requests (plus the 32 public samples, audited together because they share the same
activity stream and evidence).

## Summary

| Item | Value |
| --- | --- |
| Model providers | none at run time |
| Models | none at run time |
| Model calls | **0** |
| Input tokens | **0** |
| Output tokens | **0** |
| Total tokens | **0** |
| Average tokens per request | **0** (0 / 518) |
| Estimated total cost | **$0.00** |
| Estimated cost per request | **$0.00** |
| Wall-clock time | about 40 s on a laptop CPU (Windows 11, Python 3.14) |
| API keys needed | none |

### Per-model totals

| Provider | Model | Calls | Input tokens | Output tokens | Cost |
| --- | --- | --- | --- | --- | --- |
| (none) | (none) | 0 | 0 | 0 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **$0.00** |

## Why the run needs no model

Every decision in the run is deterministic code over participant data:

| Task | How it is done |
| --- | --- |
| Request intent (8 languages) | Rule classifier with keep/do-not-delete cues taking precedence (`code/ghost/intent.py`) |
| Identity history | `user_identities.csv` + `user_audit_log.csv`, rolled back to `audit_at`; email hashes verified as sha256(lowercase email) |
| SQL / JSON / logs / text | Parsed directly; SQL executed in in-memory SQLite |
| PDFs | Text extracted with PyMuPDF |
| Screenshots (11 PNGs) | Read from cached transcriptions (below) |
| Linking, policy, status, steps | Rules in `code/ghost/` |

## Screenshot transcription (one-time, development only, not part of the run)

The 11 screenshots under `dataset/evidence/images/` contain tables, panels and activity feeds
that local OCR (Tesseract) reads poorly. To avoid a paid vision API on every run, they were
transcribed **once** during development. This was done by the coding agent itself (Claude
Code, model `claude-opus-5-5`, Anthropic) inside the development chat: verbatim text, no
interpretation.

- Results are stored in `code/cache/images/<image>.txt`, one file per screenshot.
- Each file carries `# Source-SHA256:` of its image. The parser refuses a transcription whose hash does not match the image, so a changed image can never be read with a stale transcription.
- This work ran inside the interactive coding session (logged in `log.txt`), not through an API call made by the solution. It was not metered separately, so we do not report a token count for it.
- It costs nothing at run time and is never repeated.

If the screenshots changed, the cache check would fail loudly, and they would need to be
transcribed again (by hand, or by an optional vision-model step reading its key from an
environment variable). No such step exists in the submitted code, and no key is used or stored.
