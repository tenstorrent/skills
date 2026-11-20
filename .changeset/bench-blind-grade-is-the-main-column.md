---
"tt-project": minor
---

The daily model-bench suite grades every standard cell by blind review as the MAIN column: the
first configured reviewer (`bench.blind_reviewers`) decides would-land (score >= 2/3 of the rubric
max) and the blind score share becomes the booked score; held-out test pass rate stays as a second
column. Extra reviewers are report-only: each row records every reviewer's verdict, spend and
agreement rate with the main reviewer, and an extra reviewer's pass never overrides the main
reviewer's fail. Reviewers configurable per project; shipped default is the claude CLI's default
model at high effort. A chat-provider reviewer runs through the chat bridge started from the
project's settings (`bench blind review --config <project-or-path>`), so the open model's id never
reaches the CLI vendor.
