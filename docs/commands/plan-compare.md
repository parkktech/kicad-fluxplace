# `plan-compare` - two plan verdicts side by side

```
fluxplace plan-compare MAP_A MAP_B
```
Both maps must come from `plan-route` (or `place --objective plan`). Prints PLAN_SCORE, result, mode, every score component and
every finding count for A and B with the delta, which is better, and how many findings are only in A / only in B / shared.
