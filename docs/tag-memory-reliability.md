# Tag extraction reliability and conflict costs

Extraction jobs freeze their preceding batch, continuation allow-list, messages and context. Prompts and parsers use exactly the same string tag IDs. Older context grants no continuation permission. At most two targeted structural corrections follow the initial generation; exhaustion leaves a recoverable failed job and unconsumed messages. Legacy jobs freeze once on first resume and are explicitly marked as such; their original prompts cannot be reconstructed retrospectively.

Conflict checks use shared old/new definitions and explicit directional pairs. Every pair requires an explicit result, including coexist. Missing items remain pending. Exact content with identical provenance is the only locally resolved duplicate; lexical overlap orders candidates but never excludes same-subject/category pairs. Ambiguous contradictions, temporal preference changes and differing contexts remain semantic model decisions.

Pair caches include session, content, provenance, available time information, revision, status, template and rule version, and expire after 14 days. Partial results and received responses are checkpointed before database-backed writes. Commit verifies both revisions and candidate membership, and atomically persists tags, conflicts, provenance, consumption and commit audit.

Authenticated task diagnostics: `GET /api/webui/tag-memory/{session_id}/extraction-jobs/{job_id}/trace`. Recovery uses the existing `POST /api/webui/tag-memory/{session_id}/extraction-jobs/{job_id}/retry` endpoint. Single response usage is deduplicated by request ID; cumulative snapshots must not be added again. Unknown usage and unmeasured provider attempts remain explicit. This task audit does not depend on the separate Decision Replay feature.

The tag database automatically upgrades from schema 8 to 9 on initialization. No configuration or dependency change is required. Restart the bot to load the implementation. Failed jobs require an explicit retry and retain their messages and reference snapshots.

Offline reconstruction of 24 historical batches reduced checks from 213 to 102 and prompt characters by 28.7%, retaining all 11 recorded conflict pairs. This uses recorded decisions as fixtures, not an independent semantic quality evaluation. Exact historical status snapshots are unavailable; these reconstructed calls differ from actual historical API counts. New API token savings have not been measured. Validate actual token costs and manually labelled semantic outcomes before claiming a 50% reduction. Private logs, databases and raw replay reports are not distributed.

Release validation: 159 focused tag/Planner tests passed in the isolated release checkout; Ruff and diff whitespace checks passed. No real model or QQ tests were run for this release.
