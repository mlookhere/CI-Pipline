---
name: release
description: Prepare and validate an integration-to-production release using immutable artifacts and explicit migration/rollback evidence.
---

# Release

1. Use the supervised release profile.
2. Build once from the protected release commit.
3. Run full regression, clean-install and upgrade migrations, production-image E2E, security and dependency gates.
4. Generate the release manifest with commit, included Issues, migration set, artifact digest, SBOM, attestation, feature flags, and rollback digest.
5. Deploy only the attested immutable artifact.
6. Verify health and smoke tests. Do not call the release complete until the release Issue records deployment evidence.
