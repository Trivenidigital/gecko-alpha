# Selective dashboard retry candidate evidence

## Merged source and scope

Production baseline remains d2f0d61edc63cb55ae159ec952cce404991f21f5. Frozen priorcandidate07afc0adc27f708cd2a4ed6e9001a4baa1edb295 is unchanged. Retry adds only merged PR583 application delta: dashboard/api.py and tests/test_dashboard_websocket_lifecycle.py, from squash552db707b2f4698fba0c4f705100c2a641e066be. PR583 final head47577a02 passed CI34800882830 (8008passed,12skipped,118contracts,parity/clearances) and two integration reviews at d2c91fd0 before merge03:21:16UTC.

No full-master merge into this release. PR582 CI files and other core/config/dependency changes remain excluded. Frontend blobs are unchanged from validated07af. Exact manifest requires merged PRs575/577/578/580/583 and preserves all baseline nonallowlist files/modes and operational consumer ASTs. Retry harnessed58c424 had a discriminating red (old manifest accepted omission of583) then8passing tests.

## Reviews and local proof

Retry plan2f16606f has ops+logic approvals. Design536a80a3 has both approvals after explicitly providing the immutable sibling baseline required by the unchanged validator. Stop amendment72173c6b has both approvals,33local and33Linux mocked tests; exact final helper pin changes still require renewed reviews.

Local candidate tests:59passed (release harness8, WS/API/cold-start51), three existing installed WebSocket adapter deprecations, no cleanup failure. API/lifecycle test bytes match merged PR583; other selected sources remain final merged viewer bytes. git diff --check passed.

## Remaining release gates

The external manifest will pin this final committed SHA. Before deployment: exact tree proof; fresh candidate archive and private sibling baseline/candidate-init copies with original SHA256a0b1c9a7238ff18480060968430f6786b29875fbd1babaae3e5ddaec7d0a54e4; old-core Linux274tests including natural loopback shutdown; actual initializer/content/assets/policy/schema validation; two final candidate and helper reviews; fresh recovered-baseline runtime snapshot/preflight and locked rollout/rollback.

Original07af copy/initialization/content/269Linux-test evidence is retained for unchanged parts and is not relabeled as retry validation. Attempt1 timed out during stop, never switched Git, and existing remediator recovered the baseline; current candidate has not been deployed. Root owns fresh evidence and conditional service action. No pipeline, unit, config, account or live-trading changes.
