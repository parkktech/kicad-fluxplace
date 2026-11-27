# Limits registry (w110)
Owner rule 2026-11-27: a numeric-limit FAIL is challenged by research; the flow must stay buildable, never stricter than a sourced hard limit.

- Data: `fluxplace/data/limits.json` (tracked seed: pair_skew eth/usb2/usb3/pcie from reports/PCB pair skew limits.md and ETH skew limits line vs PHY side.md) overlaid by `~/.cache/fluxplace/limits/limits.json` (`FLUXPLACE_LIMITS_DIR`; research writes here, shared across boards).
- Entry `<rule>:<interface>`: rule, interface, unit, target, hard, basis (sourced|arithmetic|project|unverified), confidence, date, function (what breaks at which value), sources [{url,page,quote}], notes, unverified[].
- Grading (`fluxplace/limits.py: regrade`, called by review for PAIR_SKEW): project target stays the target; a project-set `skew_max_*` wins; else measured > sourced hard = FAIL (citation in the text), target < measured <= hard = WARN `PAIR_SKEW_REGRADED` with the citation. Every re-grade is a row in `<board dir>/decisions.jsonl` (`by: limits`).
- Research (`[limits] research = "on"`, model/timeout_s/budget_usd): only when a FAIL has no sourced hard value; Sonnet with WebSearch/WebFetch, cached per rule+interface (negative results too). A hard value without a URL+quote source is dropped to `unverified`: unsourced answers never loosen a limit.
- Wired: PAIR_SKEW. Not yet: POWER_WIDTH (branch current, not a limit), length budgets, clearance, NET_OVER_BUDGET, impedance tolerance.
