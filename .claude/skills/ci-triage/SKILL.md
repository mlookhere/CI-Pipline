---
name: ci-triage
description: Diagnose a local or GitHub Actions failure without blind retries. Use after a deterministic gate fails.
---

# CI triage

1. Preserve the first failing run and exact command.
2. Read the bounded output; inspect the full captured log only around the first causal error.
3. Reduce to the smallest reproducible command or test.
4. Classify as product defect, test defect, flake, dependency/toolchain issue, or CI configuration issue.
5. Fix the cause, not the symptom; never lower a threshold or skip a test solely to pass.
6. Rerun the narrow reproduction, then the owning stage. Record evidence in the existing Issue.
