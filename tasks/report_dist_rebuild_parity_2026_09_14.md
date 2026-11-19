# DASH-12 rebuild parity implementation evidence

Plan ab2a4aac and design23b0dd7e each received two independent approvals without
folds before implementation. Code candidate608f73d8ae601302ba1734f401e3e6f0797bd9a9
adds the comparator, independent CI job, focused tests and guard documentation.
No application, dist, lockfile, package, Vite configuration or runtime file changed.

## Verification

- Red: all16 initial comparator cases refused because the comparator was absent;
  the workflow test separately failed because the parity job was absent.
- Windows focused suite:20 passed,1 skipped (OS denied symlink creation).
  Black with targetpy312 and git diff --check passed.
- Root authorized new exclusive Linux scratch
  /root/gecko-dash12-validation-20260914 on srilu-vps. It was created only after
  absence check, mode0700/root. No production or release workspace was changed.
  A Git bundle preserved exact candidate608f73d8 and its committed objects.
- Downloaded Node24.14.0 Linux distribution from nodejs.org and checked its published
  SHA256. Actual npm11.9.0, Python3.12.3, Vite6.4.1. Existing lock SHA256:
  c6878003366b02faa807a7b3cc75728c1bb0459df251247e06b500bd2b6ccaec.
- Frozen npm ci --ignore-scripts --no-audit --no-fund succeeded. Two independent
  temporary output builds each transformed80 modules and matched all3 emitted
  Git blobs: index.html, assets/index-CqyQY5c1.css, assets/index-Dtb0JCcH.js.
  Vite warns the external output directory will not be emptied; each was freshly
  created by mktemp, so retaining that default is intentional.
- Linux focused suite:21 passed, including the real symlink refusal. The minimal
  test environment initially could not import the repository's unrelated app
  conftest dependencies; rerun used --noconftest -c /dev/null for these standalone
  Git/filesystem tests. Windows exercised the normal repository conftest. This
  isolated result does not claim a full application suite run.
- Real stale-source falsifier: separate disposable clone committed a rendered
  App.jsx title change, retaining old dist. Actual fresh Vite build emitted
  index-JmpnGcip.js. Existing existence tests3 passed; comparator exited1 with
  missing_committed_file for that new bundle. No forced success or active candidate
  modification was used. This distinguishes the previously missing behavior.

Local raw evidence: C:/projects/dash12-linux-proof.txt and
C:/projects/dash12-linux-resume.txt; owned remote scratch preserves fixture/output.
Initial proof stopped at the missing conftest dependency after both successful
builds; the resume log records all21 focused tests and the completed falsifier.

## Remaining gate

This report/checklist-only commit follows the verified implementation candidate.
PR582 received two independent approvals at fe034d3cb46841ebaaa2be9d5deb5fcfca777970:
ops_review covered CI/reproducibility; postmortem_integration covered structural
comparator behavior. Root relayed both terminal approvals with no folds; each
reviewer independently passed20 Windows tests with one symlink-permission skip.
The active .reviewers/582.toml records that reviewed SHA. This clearance/status-only
update preserves every implementation file. Exact final-head GitHub CI, including
the new job, remains required before merge. No deployment is authorized by this PR.
Rollback is an ordinary revert; the original Python existence guard is unchanged.
