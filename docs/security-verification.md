# Security Verification Summary

**Scope:** Historical local verification, October 24–29, 2026. This summary is not a current
scan, security certification, hosted-CI result, or acceptance of unresolved risks.

Use the [development guide](./development.md#dependency-and-image-maintenance) for operational
commands and [security and operations](./04-security-and-operations.md) for policy. Current
versions and image pins belong in package manifests, lockfiles, and Dockerfiles.

## Verification approach

- Install from the frozen lockfile with strict peer checks and the release-age policy enabled.
- Audit project dependencies and verify registry signatures.
- Verify upstream container signatures and inspect pinned image versions and architectures.
- Review builder and final runtime images separately; project audits do not cover every bundled
  build-tool or operating-system package.
- Exercise the real local HTTP path with disposable synthetic fixtures and deterministic
  providers; do not send private documents or make paid provider calls for these checks.

## Recorded results

| Date | Verification | Recorded outcome and boundary |
|---|---|---|
| October 24 | Frozen installation, formatting, lint, types, API contracts, unit tests, production build | Passed; 95 frontend unit tests. |
| October 24 | Project dependency audit and registry signatures | No known findings in that resolved project graph; 417 package signatures verified. Does not clear the package manager itself. |
| October 26 | Dockerfile validation and ARM64 production image build | Passed; Dockerfile check had no warnings. |
| October 26 | Upstream runtime signature and Node version probes | Passed on AMD64 and ARM64. Version probes are not full application journeys. |
| October 26 | Runtime-base and final ARM64 web-image scans | No known critical/high findings in those checked artifacts at that time; not a permanent zero-CVE guarantee. |
| October 26–27 | Isolated M1 HTTP journey | Passed with the browser gate explicitly skipped; disposable resources removed. |
| October 27 | Frozen installation and build after unused build-tool removal | Passed, alongside formatting, lint, types, contracts, and all 95 frontend unit tests. |
| October 27 | Targeted backend dependency update | Formatting, lint, types, all 2,627 backend unit tests, and isolated storage/API checks passed. The overall dependency audit still has unresolved findings. |
| October 27 | Migration and tracing dependency refresh | All 2,627 backend unit tests and formatting/lint/type checks passed. Five backend image targets, isolated database migrations including repeat execution, and API health/readiness checks passed. The overall dependency audit remains unresolved. |
| October 27 | Development-tool dependency refresh | Backend verification and API-contract hooks passed. Cross-process file-lock contention/reacquisition and fresh Python 3.14 environment creation passed. Changes are development-only; the overall dependency audit remains unresolved. |
| October 29 | Pinned package-manager refresh | Frozen installation, formatting/lint/types/contracts, 95 frontend tests, and 417 registry signatures passed; no known project-graph audit findings. Dockerfile check, ARM64 production build, and isolated M1 HTTP journey passed. Browser gate skipped; host build blocked by local process restrictions. |
| October 29 | Eligible Python parser and development-tool refresh | Dependency audit reported no known findings in 88 packages. Formatting/lint/types, 2,628 unit tests, parser isolation, and isolated M0/M1 HTTP journeys passed. Existing release-age and parser-version checks retained; newer upstream corrections and OS findings still require follow-up. |

## Safeguards and limitations

The frontend builder uses Corepack and pinned pnpm; unused npm/npx tools are removed. The final
non-root, read-only runtime receives only the traced application and static assets, not the
builder's operating-system tooling. This reduces exposure without claiming to contain arbitrary
malicious build code or eliminate supply-chain risk.

Some dependency and build-time findings still require follow-up. Their detailed investigation and remediation
notes are maintained locally, outside version control. Omitting those details here is a
publication boundary, not remediation, suppression, or risk acceptance. Specific release-age
exceptions are reviewed separately and must not be generalized to later dependencies or tools.

Browser acceptance remains owner-controlled. These local results do not claim that the browser
gate, a full AMD64 application build, or a public deployment passed verification. Future scans
and dependency changes can produce different results.

Do not commit raw scanner/test logs, exploit or reachability analyses, credentials, private
endpoints, personal workstation paths, full prompts, or private document content. A Git ignore
rule does not remove previously tracked files, prior commits, or published artifacts.
