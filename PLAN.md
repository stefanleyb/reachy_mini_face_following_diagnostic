# Face-Following Diagnostic Plan

## Responsibility

Provide bounded, evidence-preserving face/head measurement tools. This is a
diagnostic repository, not a product app and not a daemon-layer owner.

## Open work

- Capture one observation-only vertical trace that records face detection,
  timestamps, vertical coordinate and head pitch without changing body-control
  behavior.
- Keep raw generated sessions outside this source repository under the
  workspace `runs/face_following/` area.

Interpretation and cross-repository decisions belong in the active face/body
workstream brief.
