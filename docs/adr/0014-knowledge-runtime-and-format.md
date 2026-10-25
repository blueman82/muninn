# ADR 0014: Pinned validation runtime and distinct knowledge format

Status: Accepted by the owner, 2026-10-11.

## Decision

Use Python 3.13+ and exactly `pydantic==2.14.0`, `pydantic-core==2.50.0`,
`annotated-types==0.8.0`, `typing-extensions==4.16.0` and
`typing-inspection==0.4.4` for strict knowledge validation and schema generation.
Permit only their five import roots in runtime/installer dependency enforcement.
Preserve every other engineering standard and local code-only diagnostic rule.

`requirements-runtime.lock` contains SHA-256 hashes of PyPI universal wheels and
CPython 3.13/3.14/3.15 wheels. Provision the venv at the final immutable release
path with `--require-hashes`, `--only-binary=:all:` and the primary PyPI index.
Verify exact installed versions, a standalone strict `BaseModel`, rejection of
coercion/extra fields and JSON schema generation before activation. Bootstrap
must be independent of knowledge models that are implemented later.

Prepare candidate source/runtime before pause, snapshots or activation.
A failed candidate leaves active release, provider configuration and store
unchanged. Retain partial candidates for diagnosis; an existing venv is verified
and reused without mutation. Invalid cached runtimes fail closed. Never rename
a provisioned venv or its release ancestor. Delete superseded provisioned
releases in-place only after verification and the rollback boundary; preserve
existing reversible pruning for legacy releases without venvs.

Discover the selected release interpreter at runtime. Preserve existing fresh
and upgrade modes. Install the hashed runtime separately before developer
requirements; developer requirements do not include the runtime lock.

The owner also selected a new knowledge format starting at schema 1 with a
distinct format marker. That cutover is separate work; this runtime change does
not reset the current store, retire migrations or import old knowledge.

Implementation update (2026-10-22): `--upgrade` converts stores matching the
schema v3 table contract into the marked format. It retains the complete old
database as a private pre-upgrade archive, copies indexed history, reapplies
current erasure, and leaves the legacy knowledge ledger in the archive. The
conversion is keyed to the on-disk schema contract, not the installed release
commit. Unknown tables or incompatible copied columns refuse the conversion.

## Rationale and consequences

Pydantic supplies strict typed validation and matching JSON schema without
building a second validator. Hashed wheels bound the approved dependency
closure and prohibit unverified source builds. Existing documentation described
stdlib-only runtime imports, but S6 previously enforced import shape alone;
this decision deliberately adds the missing narrow dependency enforcement.

Venv scripts embed their creation path, so provisioning precedes activation at
an immutable location. Reusing a valid same-commit release avoids renaming a
runnable venv. A broken cached candidate needs explicit operator recovery.
Platforms/interpreters without a locked compatible wheel fail provisioning;
native installation and service behavior require separate host verification.
