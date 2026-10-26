# Capabilities and limits

All entries describe the reviewed source design, conditional on matched host/plugin, enabled actor policy and separately validated deployment. See [VERIFICATION](VERIFICATION.md) for evidence, not implied feature-wide acceptance.

## QQ observation and scheduling

Ordinary non-addressed group messages can be recorded as typed quiet observations without dispatching a main model turn. Addressed requests use native queue/ingress/history rather than treating observations as new requests. Background replay is bounded and must not replay the current addressed message as both request and background. Per-group auxiliary admission for non-mentions is optional and tool-free; it decides whether to admit a normal turn, not permission to perform actions. Images and files retain their own types and trusted source metadata; this is not unlimited historical search. Outgoing receipt storage and controlled own-message recall are internal runtime facilities, not a promise of a universally exposed recall chat tool or unlimited-time platform deletion.

Before auxiliary admission, verified bot mentions take the existing addressed path. Otherwise a nonempty platform `mentions` list or a raw `<@id>` / `<@!id>` token anywhere in the current message makes it observation-only, even if it names the assistant or asks a substantive question. Missing/unresolved target identity is conservative silence; background recording and attachment caching continue. Historical mentions do not hard-block a new unmentioned question. The log reason is `mentioned_other`. Messages without mentions retain semantic admission; `group_reply_gate.context_messages` defaults to 12 recent text entries plus the separate latest message (fewer when unavailable). This is not a switch to explicit-mention-only chat.

## QQ Markdown punctuation compatibility

With `markdown_support` enabled, `QQAdapter.format_message()` performs a conservative wire-text pre-pass before splitting the response. Paired same-line `**`/`__` bold and triple bold/italic markers get an ordinary outer space only where punctuation flanking would block emphasis: for example, `**标签：**正文` becomes `**标签：** 正文`, and `前文**“引号”**后文` becomes `前文 **“引号”** 后文`. Chinese/English quotes, brackets and punctuation keep their original characters; no invisible separator is injected and Markdown is not disabled.

Group, C2C and inline-keyboard sends share this formatting path. Code fences (including unclosed/list/quote fences), inline/multiline backtick code, escapes, URL/link destinations and existing valid bold are protected by focused regressions. This is not a general Markdown parser or a repair for every malformed/nested/cross-line span. Plain-text mode retains its existing stripping behavior; the separate standalone sender still sends plain text (`msg_type: 0`). Source changes require a human-controlled gateway reload; previous messages do not re-render. Real QQ client appearance remains a separate deployment check.

## Requester files

Partitioning is by owning profile, chat and trusted requester, not by a caller-selected root. Supported actions: `info`, `list`, `search`, `read`, `write`, `mkdir`, `copy`, `move`, `organize_csv`, `import_attachment`, `export`, `run_python`, `run_office`, `delete`, `cleanup`. Write uses `text`; copy/move use `path` and `destination`; CSV uses `sort_by`/`dedupe_by` lists and writes a UTF-8 target. Overwrite is explicit. Deletion needs an explicit user request plus `confirm=true`, only for named files/empty directories; no wildcard/recursive wipe. Cleanup does not delete ordinary persistent work to make space.

| Boundary | Limit / behavior |
| --- | --- |
| Per workspace | 256 MiB, 2048 files, 4096 entries, relative paths at most 8 levels |
| Global storage | 2 GiB, 65536 entries; exports and private lifecycle metadata count |
| Ordinary file | 32 MiB; not a promise QQ can deliver that size |
| Text write/read result | Bounded writes; read uses zero-based character `offset`, `limit` 1..16384 and `next_offset`; final JSON/output limits still apply |
| Export snapshots | 7 days |
| Explicit `temporary=true` imports | 72 hours; default imports and browser results persist |
| Transport proofs | 10 minutes; up to 4 files, each 32 MiB, per association |
| Cleanup | Opportunistic requester cleanup and hourly lifecycle maintenance after initial session startup; malformed metadata/link/identity failures fail closed |

Proof expiry is authorization expiry, not automatic deletion of successfully imported work. QQ inbound caches, logs and Docker images are outside these workspace quotas/cleanup. Large lists/searches may be truncated; organize files into directories. A successful export registers a pending snapshot for final delivery, not automatic export of all inputs/browser artifacts. Re-sending an existing work file requires a fresh export for that turn. Screenshots are delivered as documents preserving original PNG bytes where the platform supports it.

### Text is decoded, not encoding-certified

Read/search/CSV paths support UTF-8, BOM-marked UTF-16/UTF-32 and a GB18030-compatible fallback. `encoding`/`encoding_detection` describe the chosen decoding path; a successful fallback is not proof of the original encoding. Binary-looking/invalid text can return `text_decode_error`, not a permission error. Read/export do not silently transcode the source; explicit CSV output is UTF-8. Python document input remains strictly UTF-8, regardless of read fallback support.

## Offline Python

20-second execution; interface code limit 32 KiB (the lower-level transport ceiling is not a larger public allowance). At most 32 selected `.txt/.csv/.json/.md` UTF-8 documents, each 256 KiB, combined 1 MiB. Prefer explicit `inputs`; `[]` supplies no documents, omission attempts ordinary work files subject to limits. Only new/modified valid documents are saved; guest deletions are not synchronized to persistence. No arbitrary binary/image processing allowance follows from 32 MiB storage.

Fresh Linux Docker execution: nonroot, read-only root, network disabled, no host mounts/Docker socket, bounded CPU/memory/PIDs/tmpfs/files/output and no guest subprocesses/threads. The configured image must provide `python3`/stdlib. No on-demand dependencies, caller-controlled image/flags, automatic pulls or host fallback. Persistent files live in host application storage, not in a long-running container.

## Isolated office commands

`run_office` runs bash commands, Python and bounded child processes/threads in a separate pinned image. It supports ZIP/PDF and Word/Excel/PowerPoint tools via LibreOffice and Python document libraries; it is not host shell access. The old `run_python` limits above remain unchanged. `inputs` are requester-relative filenames; omission snapshots ordinary work files, `[]` means no input. Timeout is 1..90 seconds (default 60), command at most 32 KiB; exchange permits at most 256 files, 32 MiB each / 128 MiB combined. Use explicit `overwrite=true` for changed existing files. Failures, unsafe paths/links, over-budget output or unconfirmed cleanup do not save outputs; guest deletions are not mirrored into persistent storage.

Fresh nonroot containers use no network or host mounts, read-only root, dropped capabilities, no-new-privileges, 1 CPU / 1 GiB / 128 PIDs and bounded tmpfs. A protected PID-namespace supervisor stops/reaps guests before collection. Containers share the host kernel: this is not absolute isolation. Only selected files/commands enter the executor, not full chats, private profile context or credentials. Scanned PDFs may need separate OCR, which is not promised; complex Microsoft Office layouts/macros are not guaranteed compatible. See [office runtime](../plugins/guest-workspace/office-runtime/README.md).

## Anonymous public browser

45 seconds; at most 8 actions and 8 artifact files, each 32 MiB, combined 64 MiB. Broker budget: 128 fetches / 64 MiB response bodies total including downloads; at most two concurrent calls per host process. Read text is capped at 32 KiB UTF-8 and up to 80 current anchors. Scroll range -2000..2000, link indices 0..79 and eight actions maximum. Fixed 1280×720 viewport PNG, not arbitrary full-page capture. Artifact basenames are ASCII, at most 67 characters; only txt/csv/json/md/pdf/png/jpg/jpeg/webp suffixes, no paths/device names/collisions. A suffix is not content validation.

This is not the administrator's browser.
Main responses are classified before Chromium navigation: download PDF documents explicitly and parse them with the office worker instead of treating them as HTML. Navigation/TLS/public-network/HTTP errors are classified without exposing internal tracebacks. For pure read actions only, a navigation timeout can return actual server HTML with `rendering=static_html` and an explicit warning; this never claims completed JavaScript, click success or a screenshot. Missing/currently expired attachment proof is not repaired by filename/hash guessing; reuse a previously persisted requester-owned copy where possible. New ephemeral context per call; no accounts, cookies from host, forms, POST, uploading, arbitrary code or persistence. [Security](SECURITY.md) documents DNS pinning/SSRF safeguards and the disabled inner Chromium sandbox caveat.
