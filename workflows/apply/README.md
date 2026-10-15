# apply

Apply to a kept opportunity, round by round, until it's submitted or needs you.

```bash
python3 workflows/apply/run.py start <opportunity id> [--resume <id>] [--dry-run] [--headless]
python3 workflows/apply/run.py continue <application id> [--dry-run]
python3 workflows/apply/run.py submit <application id>          # approve a filled company-portal application
python3 workflows/apply/run.py regenerate <application id> <item> [--steer "more on payouts"]
python3 workflows/apply/run.py status <application id>
python3 workflows/apply/run.py summary [--hours 24]             # applied, answers chosen for you, skipped
```

## What the model sees

Your resume text and saved answers, **minus contact details**: name, email,
phone and LinkedIn URL are replaced with placeholders in the stored resume
text, left out of the saved answers it gets, and scrubbed from every prompt
as a last check. Forms get those fields from the answer bank on your machine.

## Easy Apply is hands-off

Easy Apply never waits for you. Each unknown field is resolved without asking,
and a job is **skipped (and you're notified) rather than answered with a guess**:

| Field | Hands-off resolution |
|---|---|
| New wording of a question you've answered | matched to your saved answer when the suggestion is ≥ 85% confident, and remembered |
| Required long-form question | drafted from your resume and used as-is |
| Screening question you've never answered | answered by the model from your resume, saved answers and job preferences, if ≥ 80% confident; otherwise **skip the job** |
| Saved answer worded differently from the options | the model picks the option meaning the same as your saved answer; otherwise skip the job |
| Self-identification question | your saved answer, mapped to the form's equivalent option; nothing equivalent, or never answered: the form's "prefer not to say" (skip only if it has none) |
| Optional question nobody's answered | left blank (and queued in the inbox for next time) |
| Required upload other than the resume | skip the job |

**Resume on Easy Apply:** the agent selects the resume from your LinkedIn saved
resumes whose name contains the exact file name set with `tracker.py resume
linkedin-name <id> "<file name>"` (extension included, so a .docx never
stands in for the .pdf), and confirms it's selected. It never uploads. If
that file isn't in the list, the job is skipped. Resume fields are never
answered by the answer bank or the model. Company portals upload the
resume's file instead.

After each application a notification says "Applied" or "Skipped … because …".
`summary` lists every answer chosen for you (with the model's confidence and
reasoning) so you can correct one for next time: answer it in the bank or
inbox and it's used from then on. Company portals keep the approval step
below.

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
review step. **Company portals stop filled but unsent** (`ready`): `status`
shows every value the portal will receive and a full-page screenshot;
`submit` refills from those same answers and submits. Workday-style portals
and unsolved CAPTCHAs come back to you with the link; after applying by
hand, mark it with `tracker.py application status <id> submitted`. When something needs you, the output lists each item with the
exact command to run (approve/edit/regenerate a draft, resolve or answer an
inbox question, set a bank key), then `continue`.

Approved long-form answers join the tracker's library and are offered to the
drafter as voice examples for similar questions. Inbox answers become bank
aliases, so a question is only ever asked once.

Working files per application (packet, last report, drafts) go to
`data/<application id>/` (gitignored).
