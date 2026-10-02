# Design

## Source of truth
Status: Active. Date: 2026-10-03. Surface: translation-agent local Streamlit
workbench, architecture and review page. Evidence: the user's terminal-style
agent-tree reference, streamlit_app.py, app_pages/jobs.py, application_service.py,
pipeline_graph/book.py, frontend_runtime.py and knowledge-base evaluation reports.
The user selected project architecture and task review as the scope.
The 2026-10-03 correction requires actual monospace character art, replacing
the initial card grid with the supplied reference's terminal hierarchy.

## Brand
An inspectable engineering workbench: calm, precise and evidence-oriented.
Trust comes from linked source modules, timestamps and explicit unknown states.
Avoid decorative confidence bars, fabricated activity and unrelated model prices.

## Product goals
Show the project's input, translation, publication, knowledge-base and review
boundaries in one map. Let users inspect node responsibilities and recorded run
evidence without reading every source file. Support selecting a real task or
published workspace. Success means a user can locate a failing/unchecked stage
and identify its source and evidence. The first version does not edit model
configuration, launch work or control Codex subagents.

## Personas and jobs
The repository owner inspects architecture, traces tasks and reviews changes.
They work locally on Windows, prefer Chinese labels and keep credentials local.

## Information architecture
Add an “架构与审查” navigation page alongside new task, jobs, artifacts and settings.
The page has a scope selector, architecture canvas, node details and evidence
timeline. The canvas groups orchestration, document processing, publication,
knowledge management and independent verification.

## Design principles
Use the screenshot's hierarchy and color-coded node outlines; map them to actual
project modules. Distinguish configured structure, recorded execution and review
evidence. An absent record is “未记录”, never “通过”. Show snapshot timestamps.
Read-only inspection must work before the first task is created.

## Visual language
Canvas background #11191f; text #e6edf3; muted #99a9b5. Cyan #7ddde8 marks
orchestration, blue #93b8ed processing, green #79d49f knowledge, and purple
#bea0ec verification. Use Cascadia Mono/Consolas monospace and a fixed 118-column
character canvas, centered on desktop with a 12–15 px adaptive monospace size.
Draw frames and arrows with Unicode box-drawing characters;
Chinese/fullwidth glyphs occupy two cells, combining marks occupy zero cells.
The reference hierarchy is a tall review rail, centered main/input layer,
three module lanes, publication/verification merge and bottom session log.
HTML and TXT must project the same character layout. Keep the existing
workbench theme outside the canvas. No continuous animation or CSS card grid.

## Components
Character-framed architecture node with responsibility, module identity and status marker; connector;
scope selector; selected-node details; evidence row; timestamp; unknown/empty
notice. A standalone HTML canvas may be embedded using existing Streamlit
components. It owns only its local styles, and must escape all supplied data.

## Accessibility
Target WCAG 2.2 AA. Nodes are real keyboard-focusable buttons with visible focus;
color is supplemented by labels. Keep contrast readable, respect reduced motion,
provide textual details and avoid hover-only controls.

## Responsive behavior
Desktop: a centered character tree with a review rail. Narrow screens: preserve
character alignment in a horizontally scrollable canvas; wrap details below
the canvas. The same node
actions work with keyboard, mouse and touch.

## Interaction states
Initial load shows static structure plus bounded local evidence. Empty task lists
still render the architecture. Malformed/missing state files show readable errors.
Refresh is explicit; no background model requests or heavy corpus hashing.
Selecting a node updates its details, and selected-node state is visually clear.

## Content voice
Chinese, short and concrete: “职责”, “来源”, “最近记录”, “需复核”, “未记录”.
Technical IDs belong in details. Do not present proposed components as deployed.

## Implementation constraints
Reuse Streamlit and Python standard libraries, with no new dependency. Put data
collection in a pure, independently testable module. Bound file reads and query
results, use SQLite for indexed job data, and whitelist displayed fields. Never
read .env files or expose credentials. Preserve existing page layout and routes.
Verify backend status semantics, page rendering, keyboard node selection and
desktop/narrow visual layout against a running local preview.

## Open questions
None blocking. A later user request can add controlled configuration editing or
task actions after their contracts are designed and tested.
