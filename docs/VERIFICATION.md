# Verification ledger — sanitized public candidate

## Current mention-routing fix

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
