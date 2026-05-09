# Embedded Social Agent Integration

This directory vendors the local-first `social_agent` ledger/policy engine used by
Hermes social automation. Hermes exposes it through `tools/social_automation_tool.py`
so the main agent can create dry-run social drafts, inspect the action ledger, and
run the safe executor without requiring live platform credentials.

Runtime state is not stored here. The Hermes wrapper redirects database, wiki,
raw-log, and artifact paths under the active `HERMES_HOME` profile.
