# 0017 — The MinIO server from an unmodified mirror, and provisioning without `mc`

Status: Accepted
Date: 2026-09-29

## Context

The stack pinned two MinIO images by tag and digest, `quay.io/minio/minio` for the lake server
and `quay.io/minio/mc` for `minio-init`, because their registry history had already been
unstable once (`docs/runbook.md`, image pinning). On 2026-09-28 CI's `stack` job failed on every
pull request at `make up` with `unauthorized: access to the requested resource is not
authorized`. Measured from a developer machine on 2026-09-28 and again on 2026-09-29:

- quay.io answers 401 to an anonymous pull of the pinned server digest, the pinned client digest,
  the server's amd64 and arm64 manifests by digest, and `mc:latest`.
- Docker Hub `minio/minio` and `minio/mc` answer "authentication required" at the pinned tags,
  by digest and at `latest`.

So `main` cannot pass a required check, and no pull request can merge. Only one machine still
held the images, in its local image store, and they were saved to a tarball outside the
repository before anything else was done.

The pinned server digest, `sha256:14cea493…`, is a manifest list naming three platform
manifests: arm64 `sha256:9966a92a…`, amd64 `sha256:a1a8bd4a…` and ppc64le `sha256:4a9aa577…`.
The local store held the list and the amd64 manifest with its config and nine layers, and
nothing of the other two platforms. Every blob was hashed and matched its name, and the list's
own bytes hash to the pinned digest, so the chain from the pin to every byte is intact.

## Decision

**`minio-init` provisions with boto3 in the project image, and the `mc` image leaves the
stack.** Measured against a throwaway MinIO from the pinned server image: bucket and prefix
creation, idempotent on a second run, a planted `download` policy removed on the next run
(the server's own `mc` then reports `private`), and an anonymous GET of `bronze/.keep`
answered 403. boto3 is already in the project image, which the stack builds anyway, so one
image stops being a dependency rather than becoming a second one to mirror. The anonymous check
is stricter than the one it replaces: `mc anonymous get` produced a word the script
pattern-matched, whereas `init.py` deletes the bucket policy and fails if any policy survives,
without interpreting its statements. `make health` probes the bucket through the same script.

**The server is pulled from `ghcr.io/sardorbek-suyunov/minio`, a byte-for-byte copy, pinned by
the upstream digest.** The copy was pushed with crane 0.22.1 from an OCI layout built out of
the saved blobs, then upstream's manifest list was put unchanged under the release tag.

| What | Upstream digest | Served by the mirror |
|---|---|---|
| Manifest list, the compose pin | `sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` | same, bytes hashed after an anonymous GET |
| amd64 manifest | `sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2` | same, and the list resolves `linux/amd64` to it |
| amd64 config | `sha256:69b2ec208575b69597784255eec6fa6a2985ee9e1a47f4411a51f7f5fdd193a9` | same |
| arm64, ppc64le manifests | named in the list | `MANIFEST_UNKNOWN` |

`crane validate --remote` pulled every amd64 layer back and verified its digest. The registry
accepted the list with two of its three children absent; that was not assumed, and before it
was measured the plan was to pin the amd64 manifest's digest instead, which would have changed
the pin. Compose pins the list digest, so the line differs from the upstream pin only in the
registry path, and sets `platform: linux/amd64`, because the list's arm64 entry names a manifest
the mirror does not hold.

CI's `stack` job asserts the digest after `make up`: it resolves the mirror's tag anonymously,
hashes the bytes served, compares them with the upstream digest written in the workflow rather
than read back from compose, and checks that the running server's image was pulled by it.

**Licence.** The MinIO server is licensed under the GNU AGPLv3, and the image also carries the
`mc` binary under the same licence and a Red Hat UBI 9 micro base under the UBI end user licence
agreement, which permits redistribution. The mirror redistributes all of it unmodified. The
corresponding source for this release is
https://github.com/minio/minio/tree/RELEASE.2025-09-07T16-13-09Z (commit `07c3a429`, which the
binary reports as its commit id), and for the bundled client
https://github.com/minio/mc/tree/RELEASE.2025-08-13T08-35-41Z (reported commit `7394ce0d`). The
licence texts are inside the image at `/licenses`.

## Consequences

- `main` can pass `stack` again, and so can every pull request based on it.
- The stack pulls one image fewer, and bucket provisioning is Python the rest of the platform
  already reads.
- **Frozen.** The server stays at `RELEASE.2025-09-07T16-13-09Z` with no upstream security fixes,
  because there is no upstream publishing images to take them from. The server listens on
  localhost ports of a developer machine and in a CI runner that lives for one job; nothing in
  the project exposes it further, and that is the only reason this is tolerable.
- **Dependence on the owner's registry namespace.** The stack now pulls from one person's
  account. If the package is deleted, made private or the account is lost, `make up` fails as it
  did on 2026-09-28, and the only other copy is the tarball on the owner's machine.
- **amd64 only.** The stack used to run natively on arm64 hosts. It now runs the amd64 server
  there under emulation, where the host provides it, and fails where it does not. No arm64 host
  has been measured.
- A registry that validated a list's children would refuse this copy. The mirror depends on
  GitHub's registry continuing to accept a list with absent children; if it stops, the fallback
  is to pin the amd64 manifest `sha256:a1a8bd4a…`, which is also upstream's own digest.
- The mirror carries a second tag, `list-probe`, from the measurement that the registry accepts
  the list. The package API lists it on the same version as the release tag, version 1308677835,
  named `sha256:14cea493…`; deleting it would mean deleting that version, which is the pin, so it
  stays.
- The amd64 image the list resolves to is a separate, untagged version, 1308676478, named
  `sha256:a1a8bd4a…`. A clean-up of untagged versions, by hand or by a retention action, would
  delete it and leave the list resolving to nothing on every platform. It must never be cleaned
  up.

## Alternatives considered

- **A replacement S3 server, SeaweedFS or Garage.** Rejected for now: it changes the lake in the
  middle of M4, whose acceptance evidence is taken on MinIO, and every specification from 001 to
  006 describes MinIO's behaviour. Replacing the server is deferred to M10 as an open item in
  `docs/project_state.md`, where the lake meets a cloud target anyway.
- **Building MinIO from source.** Rejected: it adds a Go build to a required check on every
  pull request, or a separately maintained build pipeline, to produce an image that has already
  been published and that this copy reproduces exactly.
- **Mirroring `mc` as well.** Rejected once boto3 was measured to do its work: a second frozen
  image in the owner's namespace, for work the project image already does.
- **Pinning the amd64 manifest digest.** It is upstream's own digest too, and would not depend on
  the registry accepting a list with absent children. It lost because it changes the pin, so
  checking the copy against the upstream record takes one more step; it remains the fallback.
