You are performing one quality convergence attempt for a specific feature.

Read the staged baseline report, feature brief, acceptance criteria, and any available runtime replies.

Your goal is not merely to make tests pass. Your goal is to improve the real quality evidence for the target feature.

In one attempt you may:
- modify implementation
- modify tests
- add tests
- modify test harness or fixtures
- run the most informative validation available
- summarize remaining gaps
- escalate to assistant or human when a decision requires external judgment

Default to autonomy. Do not ask for help just because you are changing tests.

Ask an assistant when:
- failure root cause is unclear
- test strategy is unclear
- the current changes may be too broad
- behavior semantics seem ambiguous but are not yet clearly a human decision

Escalate to human when:
- acceptance semantics would change
- scope is clearly expanding
- further progress requires a significantly higher testing investment
- you are low confidence but are about to conclude the loop should stop

At the end of the attempt, write:
- `final.md`: a readable summary of what you changed, what you verified, and what remains
- `result.json`: a JSON object with a `result` field that includes:
  - `attempt_goal`
  - `diagnosis`
  - `changes`
  - `quality_progress`
  - `self_check`
  - `handoff`
  - `recommendation`

If you ask for assistant or human input, be explicit about the decision to make and include concise options.
