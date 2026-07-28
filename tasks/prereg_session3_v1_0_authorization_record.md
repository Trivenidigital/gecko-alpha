# Session 3 v1.0 — authorization and dependency record

**New primitives introduced:** NONE (administrative record only).

**Artifact:** `tasks/prereg_session3_evidence_ruling_v1_0.md` — the Session 3
Evidence Evaluation and Product-Ruling Pre-Registration v1.0, placed as a
BYTE-IDENTICAL copy of the reviewer-approved frozen candidate. Its content
header reads "v0.4, final design round" by design: the reviewer approved that
exact byte content as the v1.0 candidate, and no edit (including a version-
string edit) is authorized — version identity is carried by this record and
the hash, not by mutating the artifact.

**Authorized SHA-256 (complete):**

```
5de1250afc967a35a7f10834d4c8f05927b2624279af87eeec3c6533badc70b4
```

Any byte or semantic change requires a new hash, reviewer review, and
product-owner authorization.

**Governing dependency:** the artifact is subordinate to the Forward Cohort
Pre-Registration v0.6 (`tasks/prereg_forward_cohort_v0_6.md`, established on
master by PR #476, merge commit `803b498f`, 2026-07-28T20:07:54Z). Session 3
redefines no Session 2 endpoint, clock, cohort, price, coverage class, or
denominator; conflicts resolve to v0.6.

**Product-owner authorization (Authorization 2, issued 2026-07-28 by the
human product owner via the direct statement "I authorize and approve both
Workstreams A and B", adopting the transmitted text; recorded verbatim):**

> I authorize the Session 3 Evidence Evaluation and Product-Ruling
> Pre-Registration v1.0 document candidate whose complete SHA-256 hash is:
> `5de1250afc967a35a7f10834d4c8f05927b2624279af87eeec3c6533badc70b4`
> This authorization is documentation-only and is conditional on PR #476
> first establishing the exact governing Forward Cohort v0.6 source in the
> repository without semantic change.
> The authorized Session 3 artifact must remain byte-identical to the
> reviewer-approved candidate. Any byte or semantic change requires a new
> hash, reviewer review, and product-owner authorization.
> This authorization completes only the human product-owner portion of
> S3-G1. It does not authorize S3-G2, report-generator implementation,
> synthetic validation, production reads, dataset freeze, report
> publication, Session 3 execution, product recommendations, deployment,
> cohort accrual, or any GA-3–GA-6 unpark action.
> All later Session 3 gates require their own explicit authorization and
> evidence.

**Materialization direction (2026-07-28, issued by the human product owner
in the dev session; the dev session recorded its reading of the direction as
issued-with-intent, open to owner/reviewer correction):** repository
materialization only — placement of the byte-identical artifact, this
record, hash verification, and a documentation-only PR for reviewer
verification. Nothing further: no S3-G2, report-generator implementation,
synthetic validation, production reads, deployment, dataset freeze, report
publication, evidence evaluation, product recommendations, Session 3
execution, cohort accrual, or GA-3–GA-6 unpark action.

**Reviewer design approval:** granted 2026-07-28 on the frozen candidate
(design portion of S3-G1). This merge remains held until the reviewer
verifies the repository artifact and checksum evidence.

**S3-G2 obligations recorded for the later, separately authorized
implementation:** (a) high-water-mark cut predicates are sufficient only if
every relevant source is append-only or historically versioned; any
mutable-pre-cut-row source requires a true point-in-time snapshot, and a
backup containing mutated pre-cut rows must FAIL synthetic validation;
(b) the freeze writer barrier may contain only bounded marker reads and the
identity-set commitment — no snapshot materialization while production
writers are blocked.
