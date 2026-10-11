# apply

Apply to a kept opportunity, round by round, until it's submitted or needs you.

```bash
python3 workflows/apply/run.py start <opportunity id> [--resume <id>] [--dry-run] [--headless]
python3 workflows/apply/run.py continue <application id> [--dry-run]
python3 workflows/apply/run.py regenerate <application id> <item> [--steer "more on payouts"]
python3 workflows/apply/run.py status <application id>
```

Each round: the tracker builds a packet of every answer known so far → the
apply-agent walks the form and stops at the first unknown field → each
unknown field is resolved and saved as an application item:

| Field | Resolution | State |
|---|---|---|
| Required long-form (textarea) | drafted from your resume by answer-drafter | `needs_review` |
| Optional long-form | left blank | `skipped` |
| Question the bank answers (exact wording or confirmed alias) | bank answer | `filled` |
| Bank key with no answer / always-ask | you answer | `needs_answer` |
| New question, required | inbox + suggested key | `needs_answer` |
| New question, optional | inbox (for next time); left blank now | `skipped` |
| Bank answer fits none of the field's options | you answer for this form | `needs_answer` |

If nothing needs you, the next round runs straight away. **Easy Apply
submits on its own** once every field is answered; `--dry-run` stops at the
review step. When something needs you, the output lists each item with the
exact command to run (approve/edit/regenerate a draft, resolve or answer an
inbox question, set a bank key), then `continue`.

Approved long-form answers join the tracker's library and are offered to the
drafter as voice examples for similar questions. Inbox answers become bank
aliases, so a question is only ever asked once.

Working files per application (packet, last report, drafts) go to
`data/<application id>/` (gitignored). External application portals come
next; until then they finish as `failed` with a note.
