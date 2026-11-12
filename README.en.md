# Hermes QQ Bot Suite

[简体中文](README.md)

**Experimental source patch + local plugin, not a standalone application or a universally installable extension.** This is an independent project, not an official Nous Research release. It targets only [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) commit **`ab44d8acf7bca230aea8574f2f2ab4666ca293ce`**. Other revisions require review and porting.

## What is included

- QQ group observation with typed quiet text/image/file records, bounded background replay, optional per-group auxiliary-model admission for non-mentioned messages, and image handling. Messages mentioning other members only are observation-only: the auxiliary model cannot override the addressee. Verified self/mixed mentions retain normal addressed handling.
- Native queue/history integration, outgoing send receipts, and controlled recall of the bot's own confirmed messages. An internal recall method is **not** automatically a model-callable tool; availability and authorization must both be checked.
- QQ Markdown bold punctuation compatibility: repair literal `**` when colons, Chinese/English quotation marks or brackets touch adjacent prose. Insert ordinary spaces only where needed before chunking, without changing full-width characters, code or link destinations.
- [Contextual GIF expressions](docs/MEMES.md) (Chinese guide): primary-model selection from reviewed IDs, GIF-only replies or text followed by a separate GIF, durable delivery feedback, and queue/compression continuity. Text-bearing decorations use PRD; GIF-only replies skip decoration probability/cooldown while retaining safety and quota limits. Stable per-event candidate snapshots expose recent successful-send counts as soft diversity hints only. An optional legacy background-selection mode is retained. Disabled by default; no private image collection is included.
- Ordinary QQ inputs receive independent FIFO positions regardless of administrator identity: no steering, interruption or media merging. Explicit control commands remain separately authorized. The [full-source audit](docs/SOURCE_SYNC_AUDIT.md) distinguishes ported changes, independent public differences and deferred newer-upstream interfaces.
- Request-actor tool/command gates and public/private context separation. A nickname, prompt label, shared session, or home delivery target never grants administrator rights.
- Requester-scoped persistent files, trusted attachment import, text/CSV operations, explicit export and transport delivery accounting, lifecycle cleanup, offline Docker Python, and a restricted anonymous public-page browser.
- Isolated office commands (`run_office`) for binary ZIP/PDF/Word/Excel/PowerPoint work, using a separately provisioned Docker image with LibreOffice and Python document libraries. This is not host shell access or a relaxation of the small-text Python tool.

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

This expression update passed **76 test files: 1504 passed, zero failed, one POSIX-only skip** on a clean pinned Windows host: 60 manifest offline files plus 16 upstream queue, receipt, streaming/TTS and prompt-cache regression files. Eight separate release-tool tests also passed. The manifest lists 141 overlay files and 64 test files; four external Docker/browser environment files were not run this time. Previous results remain historical and are not added to this count. See [VERIFICATION](docs/VERIFICATION.md). Source and offline outbound-boundary tests are not Linux/macOS CI, fresh Docker recipe builds, real-model/QQ client animation or delivery proof, or security certification.

## Licensing and upstream

Upstream Hermes Agent is by Nous Research and contributors and is MIT-licensed. Preserve its notices and the repository LICENSE when redistributing derived source. Playwright, Chromium, Python and container layers retain their own licenses/notices. No third-party wheels, browser binaries, images, raw private build receipts, credentials, sessions, or deployment data are vendored here. Consult [upstream documentation](https://hermes-agent.nousresearch.com/docs/) for Hermes itself; this repository's pinned source remains the compatibility target.
