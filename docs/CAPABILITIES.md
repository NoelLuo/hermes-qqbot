# Capabilities and limits

All entries describe the reviewed source design, conditional on matched host/plugin, enabled actor policy and separately validated deployment. See [VERIFICATION](VERIFICATION.md) for evidence, not implied feature-wide acceptance.

## QQ observation and scheduling

Ordinary non-addressed group messages can be recorded as typed quiet observations without dispatching a main model turn. Addressed requests use native queue/ingress/history rather than treating observations as new requests. Background replay is bounded and must not replay the current addressed message as both request and background. Per-group auxiliary admission for non-mentions is optional and tool-free; it decides whether to admit a normal turn, not permission to perform actions. Images and files retain their own types and trusted source metadata; this is not unlimited historical search. Outgoing receipt storage and controlled own-message recall are internal runtime facilities, not a promise of a universally exposed recall chat tool or unlimited-time platform deletion.

## Requester files

Partitioning is by owning profile, chat and trusted requester, not by a caller-selected root. Supported actions: `info`, `list`, `search`, `read`, `write`, `mkdir`, `copy`, `move`, `organize_csv`, `import_attachment`, `export`, `run_python`, `delete`, `cleanup`. Write uses `text`; copy/move use `path` and `destination`; CSV uses `sort_by`/`dedupe_by` lists and writes a UTF-8 target. Overwrite is explicit. Deletion needs an explicit user request plus `confirm=true`, only for named files/empty directories; no wildcard/recursive wipe. Cleanup does not delete ordinary persistent work to make space.

| Boundary | Limit / behavior |
| --- | --- |
| Per workspace | 256 MiB, 2048 files, 4096 entries, relative paths at most 8 levels |
| Global storage | 2 GiB, 65536 entries; exports and private lifecycle metadata count |
| Ordinary file | 32 MiB; not a promise QQ can deliver that size |
| Text write/read result | 128 KiB bounds; final JSON/output limits still apply |
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

## Anonymous public browser

45 seconds; at most 8 actions and 8 artifact files, each 32 MiB, combined 64 MiB. Broker budget: 128 fetches / 64 MiB response bodies total including downloads; at most two concurrent calls per host process. Read text is capped at 32 KiB UTF-8 and up to 80 current anchors. Scroll range -2000..2000, link indices 0..79 and eight actions maximum. Fixed 1280×720 viewport PNG, not arbitrary full-page capture. Artifact basenames are ASCII, at most 67 characters; only txt/csv/json/md/pdf/png/jpg/jpeg/webp suffixes, no paths/device names/collisions. A suffix is not content validation.

This is not the administrator's browser. New ephemeral context per call; no accounts, cookies from host, forms, POST, uploading, arbitrary code or persistence. [Security](SECURITY.md) documents DNS pinning/SSRF safeguards and the disabled inner Chromium sandbox caveat.
