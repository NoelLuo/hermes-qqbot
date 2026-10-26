# Verification ledger — sanitized public candidate

## Current Markdown punctuation / quotation-mark update

Only the reviewed Markdown import/formatting hunks were migrated into the neutral public adapter; its assistant label and `gateway.trust_env` proxy opt-out were retained. The new sibling module and regression file are included in the overlay, manifest test list and regenerated patch. The patch applied to a fresh disposable checkout of the exact pinned upstream commit, and `prepare_host.py` read back **all 113 overlay files** as `already_matches`. This checkout shares immutable Git objects, not working files, with the local source tree. No live configuration or service was changed.

Patch SHA256: `a8304ef2bab36bab94235e36554082e3fdff9ff9242b8bb40f9e42efaae0f095`.

Canonical Windows / prepared PM Python 3.14.7 focused run: **6 files, 173 passed, 0 failed**, 37.7 seconds, three workers, no file retries, explicit QQ bootstrap and real-home protection retained. Files: `tests/gateway/test_qqbot_markdown.py`, `test_qqbot.py`, `test_qqbot_local_migration.py`, `test_qqbot_receipt_integration.py`, `test_qqbot_delivery_receipts.py`, and `tests/plugins/platforms/test_qqbot_send_targets.py`. The new file contributes 49 cases. The public candidate has its own fixture set; this result is not copied from the private deployment's count.

The new tests check strict CommonMark rendering, character preservation, idempotence, group/C2C and keyboard outbound payloads, pre-chunking normalization, and preservation of code, escapes, URLs and valid bold. Eight separate release-tool unittest cases also passed. Scoped Ruff passed for the new source/test using the pinned host configuration. A final bounded 149-file hygiene scan, including a private deny list outside the project, had no findings; changed documentation's relative links were checked. This is not comprehensive secret/security certification. These are offline tests: no QQ message, live auxiliary-model request or gateway restart was performed. Actual QQ client rendering remains unverified. The remaining manifest tests and Docker/browser/office runs were not rerun for this narrow increment; the full manifest lists 52 test files. Previous results below are historical, not added to the current total.

## Previous isolated-office and browser update

The reviewed increment was migrated onto the public overlay without replacing its neutral names or adding private configuration/history/attachments. The regenerated patch applied to a fresh disposable checkout of the exact pinned upstream commit; all **111 overlay files** matched their manifest hashes. The checkout shares immutable Git objects with the source repository, not working files. An explicit installation to a new isolated plugin destination was read back byte-for-byte for all 14 plugin/build files; it changed no live configuration or service. The final 146-file bounded hygiene scan, including a private deny list kept outside the project, had no findings; relative Markdown links were also checked.

Patch SHA256: `799d0402946bd2912d82a6a0910830f5fb9c1f6819a24d529134057ae30f64ec`.

Canonical Windows / prepared PM Python 3.14.7 run: **51 files, 996 passed, 0 failed, 1 skipped**, 574.1 seconds, two workers, no file retries, real-home protection retained. The sole skip is POSIX-only. The full manifest test set was exercised on this public candidate, not inferred from the private deployment's different count.

This includes actual hardened browser JavaScript/screenshot/download tests, live public GET/store/export integration, small-text Python Docker tests, and the new office live path: a generated 24 MiB binary ZIP, DOCX/XLSX/PPTX creation, PDF text extraction, LibreOffice headless conversion, persistence/export/ledger checks, failure rollback, ZIP traversal/link refusal and descendant cleanup. Controlled broker fixtures and real public requests are separate cases; no real QQ message was sent.

Release-tool tests: **eight passed**, with five additional pytest-reported subtests; these are separate from the 996 suite passes. Scoped Ruff passed for the changed overlay files using the pinned host configuration. All supplied office Python build/helper sources compiled. The twelve-wheel CPython 3.13/Linux hash lock was exercised with `pip download --no-index --require-hashes` against the previously acquired cache, with no host package installation. This validates the dependency lock, not a fresh Docker build. Public metadata/examples remain disabled and carry no usable private image ID.

Existing runtime images were used. The office supervisor/helper and browser worker were checked against public source after LF normalization; the browser image contains CRLF source, so this is **normalized source equivalence, not a raw-byte image/source hash equality claim**. The published build/activation instructions continue to require rebuilding and checking exact baked hashes for each deployment. Fresh builds of the newly supplied portable office recipe, and of every other public recipe, remain acceptance work. There is no Linux/macOS CI or real QQ delivery receipt in this ledger. Source scans are bounded hygiene, not comprehensive secret/security certification; raw host logs and images remain private.

## Previous mention-routing fix

The addressee guard and ten additional parameterized regression cases were applied to the reviewed public overlay, without private persona/configuration/history. The generated patch was successfully applied to a fresh disposable checkout at the pinned upstream commit; all 103 overlay files matched manifest hashes afterward. This checkout shares immutable Git objects with the local source, not working files.

Patch SHA256: `e0ff0ad9688cfd3a6ce1a6114087f4187ef3874d88897883157b0770e20bde99`.

Canonical runner on Windows / PM Python 3.14.7: **5 files, 30 passed, 0 failed**, 46.4 seconds, three workers, no retries, real-home guard retained. Files: `test_qqbot_group_observation.py`, `test_qqbot_reply_gate.py`, `test_qqbot_group_event_upgrade.py`, `test_qqbot_group_identity.py`, and `test_qqbot_group_images.py` under `tests/gateway/`. Tests prove other-member mentions do not consult even a reply-true model stub and remain durable observations; verified self/mixed mentions dispatch normally; raw mention fallback works; historical mentions do not block the next open question.

Scoped Ruff on the changed source/test, using the pinned host configuration, reports two pre-existing findings (`RET501` and `PLR1711`) at the unchanged test handler's `return None`; running the preceding committed fixture through the same configuration reproduces both. No new findings were introduced. The Python 3.14 release hygiene scan checked 131 files with no findings; this remains a bounded scan, not comprehensive certification.

This focused run did not call a live auxiliary model or send QQ messages. The preceding 45-file/Docker and release-tool results below were not rerun for this fix and are not added to the current total. No gateway restart or live configuration upload was performed.

## Preceding publication status (before the mention-routing fix)

**Candidate regression and installer validation passed on Windows / PM Python 3.14.7.** An independent no-hardlinks clone at the pinned upstream commit received the reviewed overlay and an isolated plugin installation. All 103 overlay files and two plugin files were read back against manifest hashes, then independently rechecked by the release owner.

Patch SHA256: `8c78e1a4a36e099f348f472b72e36f117a63a4bd5b6e1be746abe3f50af1c0fb`.

- Canonical targeted suite: **45 files, 909 passed, 0 failed, 1 skipped**, 180.0 seconds, three workers. The sole skip is POSIX-only. Actual browser Docker tests (22), public-page screenshot/store/export integration, and live Python Docker tests (four) passed and are already included in that total.
- Release-tool unit tests: **eight passed**, separate from the suite above. Default check made no source writes and kept the host clean; wrong baseline, dirty checkout and existing plugin targets were refused; explicit apply/install passed.
- The canonical host runner retained real-home protection and used the explicit QQ bootstrap, `--file-retries 0` and `--tb=short`. `verify_host.py` forwards the validated `QQBOT_TEST_PYTHON_IMAGE` through an explicit pytest option because the runner scrubs arbitrary environment variables. Run its `--help` with a prepared test interpreter; default jobs is one, this run used three.
- Strict scoped Ruff reports one existing F401 for the deliberate `gateway.run` preload in `tests/gateway/test_qqbot_agent_security.py`. With only that file's F401 explicitly excepted, scoped lint passed. No guard was disabled.
- The release owner ran bounded source/type/token/path hygiene and an additional private deny-list scan with no findings. This is not comprehensive secret or security certification. Raw machine logs remain outside the public repository.

These are **not Linux/macOS CI results or live QQ API receipts**. Existing local images were used; the newly supplied public Docker recipes have not been rebuilt for this release. The replacement Python recipe and each installation's actual QQ delivery remain acceptance work. Historical private deployment results below are not substituted for candidate proof.

Validation caught and fixed a release helper's `Path.is_junction` assumption, opt-in environment loss that initially skipped four Python tests, and an intermediate malformed pytest-option invocation. Four extra EOF blank lines were normalized and manifest hashes/patch regenerated. Release scripts are documented for Python 3.12+; complete execution here was on 3.14.7. No private runtime source, configuration or service was modified.

## Historical context, not candidate proof

The operator reports that the existing private deployment's latest focused run covered 45 files with 908 passes and one POSIX-only skip on Windows, including Docker work. This supplied historical count has **not** been rerun or attributed to the public candidate here. The operator also confirmed real QQ browser screenshot, attachment import/export and file text reading. Those observations concern that deployment and selected interactions only, not every feature or adversarial path.

The documentation author inspected runtime source/provenance and public package/registry metadata, but did not build/pull an image, change a service, or execute the public candidate test suite. No Linux execution is claimed. CI definitions, if present, are not execution receipts; live Docker/network jobs must be explicitly opted into and reported separately from offline unit tests.

## Documentation/build-input checks completed

Static checks over the authored documentation/examples/build-source set found no broken relative Markdown links and no inspected private host paths/image IDs. The four exact wheel URLs and SHA256 values were compared with public PyPI metadata. The pinned MCR manifest/config was retrieved as JSON and confirmed Linux/amd64 with no declared volumes; Docker Hub metadata supplied the official Python 3.14.6 amd64 manifest. No wheel/image layer was downloaded or built by these checks. YAML syntax checking passed for the inert example. These checks do not execute Docker, validate installer behavior, prove secret absence, or replace candidate regression tests.

## Release-owner checklist

- [x] Confirm exact upstream base, clean disposable host and sanitized overlay/dependency closure, including `tools/guest_workspace_text.py`.
- [x] Exercise script check-only mode with before/after write checks; test wrong base/dirty tree/existing plugin refusal and explicit apply/install paths.
- [x] Run the final public candidate's selected regression suite with real-home guards; report exact canonical-runner output, no overlapping count arithmetic.
- [ ] Build the public Python/browser recipes independently, verify architecture/interpreter/package versions, immutable image IDs and baked worker SHA256.
- [x] Run actual hardened Docker fixture tests, timeout cleanup and isolation tests; distinguish injected broker fixtures from live public GET.
- [x] Run optional live public-page screenshot tests and report network failures honestly.
- [x] Test guest/admin, group/DM and cross-requester/profile boundaries, quiet attachment association, typed replay, queue and send-receipt paths.
- [ ] Test actual QQ receipt/delivery with a consenting test account; `pending_transport`, export success or generated MEDIA text is not delivery proof. Recall API acknowledgement is not visual disappearance.
- [x] Review licenses, public examples, link targets, final repository contents and secrets/identifiers before publishing; a pattern scan is not a security certification.

Keep failures and skips visible. A green source test does not imply installed, enabled, loaded, actually executed or actually delivered.
