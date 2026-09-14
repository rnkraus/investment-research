# ADR-0007 — SeaweedFS for local S3-compatible object storage

**Status:** Accepted

**Date:** 2026-09-14

**Scope:** Local M0 object storage, application S3 client, and later public storage boundary

## Context

The archive requires S3-compatible storage for canonical immutable originals. Earlier plans named
MinIO for the local Docker Compose runtime. MinIO states that its open-source products reached end
of life in September 2025, and its open-source server repository is archived. Building a new
canonical-storage dependency on that server would conflict with the project's maintenance and
security requirements.

The replacement must remain simple enough for a single-developer local stack while exercising the
same narrow S3 data-plane behavior expected from a later managed service. The application must not
depend on provider administration APIs or import an SDK outside the infrastructure adapter.

## Decision drivers

- use an actively maintained local S3-compatible server without a renewable commercial license;
- preserve conditional immutable creation, checksums, streaming reads, and compensating deletion;
- keep provider code behind the existing application-owned `ObjectStorage` protocol;
- support least-privilege application credentials separated from administrative bootstrap access;
- keep the local runtime understandable and reproducible in Docker Compose;
- avoid making a self-hosted development service the default public-production recommendation;
- pin reviewed images and packages without bypassing the repository release-age policy.

## Options considered

1. **Continue with MinIO open source.** Rejected because the upstream server is end-of-life and
   archived. Digest pinning would make it reproducible but would not restore security maintenance.
2. **Use MinIO AIStor Free.** Rejected for the local baseline because its proprietary renewable
   license adds registration, renewal, and availability coupling to canonical storage and CI.
3. **Use Ceph RGW.** Mature and production-capable, but operationally disproportionate for the
   single-developer Compose environment.
4. **Use RustFS.** Promising and S3-compatible, but its current pre-stable release maturity and
   recent security history make it unsuitable as this project's canonical local baseline today.
5. **Use Garage.** Lightweight and viable for some deployments, but its smaller ecosystem and S3
   compatibility differences provide less confidence for the selected contract.
6. **Use SeaweedFS locally and a managed S3 service publicly.** Selected because SeaweedFS is
   actively maintained, Apache-2.0 licensed, supports a single-node Docker path, and implements the
   required S3 data-plane operations.

## Decision

The local Docker Compose runtime uses SeaweedFS as its S3-compatible canonical object store. A later
public environment uses a separately selected managed S3-compatible service; self-hosted SeaweedFS
is not the default public-deployment decision.

Application code continues to depend only on the existing `ObjectStorage` protocol. The
infrastructure adapter uses the maintained AWS Python SDK for the narrow S3 data plane and accepts
an explicit endpoint so the same adapter can target SeaweedFS locally and a managed service later.
Application modules must not import that SDK or provider administration APIs.

The supported adapter surface is intentionally small:

- conditionally create an object with `If-None-Match: *` so an existing original is never replaced;
- stream request and response bodies while verifying byte count and checksum;
- open or inspect an authorized object without disclosing foreign or unknown object existence;
- delete only the exact uncommitted object during narrowly scoped capture compensation;
- use server-generated, workspace-scoped keys and a private bucket.

The local topology separates administrative bootstrap credentials from least-privilege application
credentials. Only API and worker processes receive the application credentials. The web, parser,
and browser receive no object-storage credentials. Bootstrap access is restricted to initialization
and verification and is not reused by application processes.

No object-storage control or data port is published publicly. The application network may reach
only the required S3 data-plane endpoint. A single-process `weed mini` topology is acceptable only
if Docker Desktop and Linux CI prove that application processes cannot reach unnecessary control
surfaces. Otherwise M0 must split the SeaweedFS gateway and internal components or add an equally
narrow network boundary before the Step 2 gate passes.

The selected SeaweedFS image is pinned by version and digest only after compatibility, provenance,
release-age, and vulnerability review. The `latest` tag is forbidden. Because SeaweedFS supports
security fixes on its latest release while this repository applies a settling period, an upgrade is
adopted after that period only when it contains all applicable fixes. If an applicable critical or
high-severity fix arrives inside the settling period, implementation pauses for an explicit risk
review rather than choosing either an unpatched or insufficiently reviewed image silently.

### Reviewed SeaweedFS 4.47 release-age exception

On 2026-09-14, the project owner explicitly approved a narrow release-age exception for SeaweedFS
4.47. The release was less than seven days old, but the immediately preceding eligible image had
applicable critical and high dependency findings that 4.47 reduced. Waiting or selecting the older
image therefore did not provide the normally intended security benefit of the settling period.

The exception applies only to the local M0 and disposable CI image identified by the multi-platform
digest `sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882`.
Before approval:

- the 4.47 tag resolved to the same digest through Docker Hub and GitHub Container Registry;
- Cosign verified the tag against the official
  `container_release_unified.yml@refs/tags/4.47` GitHub Actions identity and transparency log;
- Docker Scout scans of the resolved Linux AMD64 and ARM64 images reported no critical findings;
- both platforms reported one high `google.golang.org/grpc` finding, whose published affected path
  requires an xDS-created gRPC server; the selected master, volume, filer, and S3 commands do not
  enable that xDS server path;
- the local ARM64 binary reported version 4.47 and source revision `c50733600`.

This is a bounded risk acceptance, not a change to the general release-age policy. The image remains
local-only, receives no private archive data, exposes no public endpoint, and must be rescanned once
seven full days have elapsed and before any public use. A new applicable finding, provenance change,
or digest change invalidates the exception and pauses adoption for another review.

This ADR supersedes only provider-specific MinIO references in ADR-0001 and ADR-0002. Their process
boundaries and canonical-versus-derived ownership decisions remain accepted and unchanged.

## Consequences

The application gains a maintained local storage service while retaining a portable S3 contract.
Provider replacement later is configuration and adapter-contract work rather than a domain rewrite.
The local stack remains materially smaller than a Ceph installation and avoids a renewable license
dependency.

SeaweedFS releases frequently and supports only its current release, so image review and regular
upgrade testing become explicit maintenance work. Its broader feature set is not implicitly
approved: only operations covered by repository contract and isolation tests may be used. A local
emulator also cannot prove every behavior of the eventual managed production service, so that
provider will require its own opt-in contract tests before deployment.

The single-node local volume improves development persistence but is not a backup, high-availability,
or production durability guarantee. PostgreSQL metadata and object state remain a two-part canonical
boundary governed by ADR-0002.

## Validation and reconsideration triggers

Before Step 2 is complete, Docker Desktop and Linux CI must verify:

1. conditional create admits exactly one winner and never replaces an existing object;
2. streamed create and open preserve byte counts and checksums;
3. unknown and foreign-workspace objects have the same externally visible result;
4. administrative and application credentials are distinct and the application credentials cannot
   perform bootstrap or unrelated bucket operations;
5. web and parser processes receive no storage credentials;
6. restart preserves committed originals, while test cleanup removes only its unique project data;
7. only the required S3 data-plane endpoint is reachable from application processes;
8. the pinned image and Python SDK pass the repository's security and release-age checks or one
   explicit, documented, and bounded exception.

Reconsider SeaweedFS if these checks fail, upstream maintenance stops, applicable vulnerabilities
cannot be mitigated promptly, required S3 semantics diverge, or local operation becomes too complex.
Select the managed public provider in a separate ADR when public deployment begins.

## References

- [MinIO legal notice and open-source end of life](https://www.min.io/legal)
- [SeaweedFS project, license, Docker path, and S3 capabilities](https://github.com/seaweedfs/seaweedfs)
- [SeaweedFS 4.47 release](https://github.com/seaweedfs/seaweedfs/releases/tag/4.47)
- [SeaweedFS security policy](https://github.com/seaweedfs/seaweedfs/blob/master/SECURITY.md)
- [gRPC xDS denial-of-service advisory](https://github.com/grpc/grpc-go/security/advisories/GHSA-2v4p-qf9q-27wj)
- [Boto3 package and support metadata](https://pypi.org/project/boto3/)
- [Amazon S3 conditional `PutObject`](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)
