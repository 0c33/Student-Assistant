# Eval fixtures: a synthetic vault (test data)

`vault/` is copied into a fresh temporary folder for every eval trial (`evals/run.py`) and for the security
tests (`security/`). Everything in it is **synthetic**: the student, courses, instructors and dates are made up.

**`vault/inbox/notes_from_classmate.md` contains a deliberate prompt injection** (the paragraph addressed to
"Assistant"). It is attack data for the injection-resistance tasks and the security test, not an instruction to
anyone reading this repository. The label lives here rather than in the note itself, so the attack the agent sees
is realistic.
