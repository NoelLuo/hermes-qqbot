# Hermes QQ Bot Suite

[简体中文](README.zh-CN.md)

**Experimental source patch + local plugin, not a standalone application or a universally installable extension.** This is an independent project, not an official Nous Research release. It targets only [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) commit **`ab44d8acf7bca230aea8574f2f2ab4666ca293ce`**. Other revisions require review and porting.

## What is included

- QQ group observation with typed quiet text/image/file records, bounded background replay, optional per-group auxiliary-model admission for non-mentioned messages, and image handling.
- Native queue/history integration, outgoing send receipts, and controlled recall of the bot's own confirmed messages. An internal recall method is **not** automatically a model-callable tool; availability and authorization must both be checked.
- Request-actor tool/command gates and public/private context separation. A nickname, prompt label, shared session, or home delivery target never grants administrator rights.
- Requester-scoped persistent files, trusted attachment import, text/CSV operations, explicit export and transport delivery accounting, lifecycle cleanup, offline Docker Python, and a restricted anonymous public-page browser.

These features depend on the **reviewed core overlay and plugin together**. Copying just the plugin does not install the suite. Public outputs remain public; a workspace is not a secrets vault or an OS sandbox.

## Start safely

1. Read [deployment](docs/DEPLOYMENT.md), [security/data flow](docs/SECURITY.md), and [verification status](docs/VERIFICATION.md).
2. Use a disposable, clean checkout of the exact upstream commit. Review the source manifest, overlay and generated patch before applying anything.
3. Run the host preparation script in check-only mode first. `--apply` is explicit; plugin copying additionally requires `--install-plugin --plugin-dir PATH` with a **nonexistent** destination. See the deployment guide for exact commands and the script's verification boundary.
4. Review [the disabled configuration example](examples/config.safe.yaml). The platform/plugin stay off, while the actor gate stays on with no public-tool grants. Supply credentials privately, grant no administrator by default, and keep browser/group auto-reply disabled until separately validated.
5. Docker images must be independently built/provisioned and verified. See [runtime builds](docs/BUILD.md); no host-specific image ID or bundled binary is portable proof.

No automated gateway restart, service installation, or live configuration mutation is part of these instructions. A human decides when to reload after validation.

## Limits and release status

Workspace: 256 MiB per requester/chat/profile, 32 MiB per ordinary file, 2048 files; global 2 GiB. Exports expire after 7 days, explicitly temporary imports after 72 hours. Attachment transport proofs last 10 minutes and authorize at most four files per association. [Full limits](docs/CAPABILITIES.md).

The sanitized public candidate passed **45 targeted test files: 909 passed, zero failed, one POSIX-only skip** on Windows, plus eight separate release-tool tests. See [VERIFICATION](docs/VERIFICATION.md) for exact scope and remaining work. This is not Linux/macOS CI, a fresh build of the published Docker recipes, real QQ API delivery proof, or security certification.

## Licensing and upstream

Upstream Hermes Agent is by Nous Research and contributors and is MIT-licensed. Preserve its notices and the repository LICENSE when redistributing derived source. Playwright, Chromium, Python and container layers retain their own licenses/notices. No third-party wheels, browser binaries, images, raw private build receipts, credentials, sessions, or deployment data are vendored here. Consult [upstream documentation](https://hermes-agent.nousresearch.com/docs/) for Hermes itself; this repository's pinned source remains the compatibility target.
