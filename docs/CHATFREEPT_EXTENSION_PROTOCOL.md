# Chat FreePT Chrome extension and CI integration — protocol v1

## Scope

Based on the user-supplied Chat FreePT v0.8.9 Chrome Manifest V3 bundle (content.js, background.js, chat-state-bridge.js). This is a compiled extension, not an editable extension source tree. Its static behavior can be inspected, but no source-controlled Chrome end-to-end tests or extension release verification can be claimed from this upload alone.

The Chrome extension controls browser conversation ownership, message sending, delayed continuations, stream-recovery and pause/resume. ChatGPT performs software planning and GitHub operations. GitHub Issues, work branches, PRs, protected branch rules, completed Actions checks and releases are durable truth. GitHub Actions only executes deterministic commands; it does not host a second coding agent.

## Observed continuation contract

The extension recognizes these statuses: Continue, Needs input, Plan ready, Testing, Complete, Error. The final assistant status marker carries V, PHASE, REPO, ITEM, NOTE, and when available BRANCH, ISSUE, PR, CI, CHECKS, ACTIONS, URL. The bundle's default template repository is mlookhere/CI-Pipline, but the selected consumer repository is the strict lock. Never change the repository lock just because the template originated elsewhere.

The extension's last completion marker triggers a fresh final audit before the run is marked done. Continue requests another turn. Plan ready starts development. Testing and Needs input pause for external validation or real user input. Error pauses. When a stream is interrupted or the marker is absent, the extension has recovery paths. Its background service worker protects one owner per ChatGPT conversation and drives wakeups; it is not a durable GitHub job scheduler.

The bundle uses a send delay, quiet periods, a configurable continuation cap, and periodic protocol refresh prompts. These are browser controls and do not imply that ChatGPT can perform uninterrupted background work. A new chat or tab may be created during handoff, so every subsequent turn must reconstruct GitHub state and avoid duplicating Issues, branches, and PRs.

## Contract and authorization

.chatfreept/extension.json is the shared versioned protocol. workflow/chatfreept_extension_protocol.py validates the v1 terminal marker as an **advisory status report**, checks selected repository and GitHub URL scope, and catches inconsistent structured counts. Even the apparently clean combination of CI: Passed and five passing reported checks **never grants merge, release, or completion authorization**. Only a fresh GitHub read of the exact head SHA, actual required checks, branch protection, approvals and the PR merge state supports those actions.

Consumer CI must preserve the correct production branch from the consumer's GitHub policy. The extension's general onboarding prompt can mention main; this template currently uses master. Do not change or force-push production because of prompt wording.

To inspect a copied terminal marker offline:

    python workflow/chatfreept_extension_protocol.py --repo owner/project --input marker.txt

This validator is not automatically invoked by the uploaded extension. Consumer extension source tests should compare the actual browser parser against the versioned fixtures before adopting a new marker version. Do not assume the presence of these Python tests means browser interaction has been tested.

## Recommended consumer pipeline gates

When used to develop the Chrome extension itself, the consumer should use a source-controlled Node toolchain with pinned versions and provide actual headless package scripts for formatting, linting, typecheck, unit tests, Chrome MV3 build and extension packaging. Configure those commands in the consumer's .chatfreept/ci.json; do not vendor hard-coded assumptions about that project's package layout.

Required integration regressions should exercise: terminal marker and alias extraction; stage transitions including final audit; a missing marker; two-tab single-owner fencing and lease expiry; delayed resend and interrupted stream recovery; browser permission/consent gating; composer recovery; and bounded continuations. Favor fake time and mocked Chrome APIs in fast tests, with an actual browser run in a separate build/test gate. Verify the manifest minimum permissions, web-accessible bridges, content-script origins, reproducible bundle and Chrome Web Store packaging policies before release.

Successful CI for this pipeline repository is not evidence that a separate consumer extension's tests passed. The consumer must have its own successful GitHub Actions runs and release gates. The initial packaged v0.8.9 upload did not contain the original TypeScript sources, package.json scripts, or automated test suite.

## Autonomous workflow

Each turn reads the existing control Issue, master plan, owning Issue, current work branch, relevant PR, exact-head Actions results and failing job logs. One slice = one Issue = one work branch = one PR. Fix the first causal failure, with bounded attempts per cause. Do not weaken checks. On a green PR, verify that every *required* check completed success on the precise head and that the protected branch allows the merge. After merging, close its Issue, update the control Issue and proceed to the next slice. Complete requires a separate comprehensive audit of open work, release and production validation.
