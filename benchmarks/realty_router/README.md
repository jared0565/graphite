# Realty routing benchmark

This versioned synthetic corpus compares `frontier_only`, `native_automatic`, and
`graphite_routed` result sets without provider access. It contains no customer data,
credentials, MLS/IDX content, or copied proprietary code.

```powershell
python -B benchmarks/realty_router/evaluate.py --results captured-results.json
```

Result records must name exact model, profile, and policy versions. The evaluator
reports sample size, USD-equivalent micro-cost, aggregate latency, acceptance,
repairs, escalations, severe failures, and a 95% Wilson lower bound. Small samples
are labeled insufficient evidence; the report does not claim delivery-speed,
quality, robustness, or cost causality.

Offline evaluation is the default and opens no provider connection. Live evaluation
is deliberately not a CI path: use Graphite's approval-gated routing service with
explicit per-run cost review and hard quotas, then supply the resulting records to
this evaluator.
