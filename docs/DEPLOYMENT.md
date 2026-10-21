# Reviewed deployment, not unattended installation

## Scope and prerequisites

Use a disposable upstream checkout at `ab44d8acf7bca230aea8574f2f2ab4666ca293ce`, Git, Python 3.12+ for these release scripts (including `Path.is_junction` checks), and the host's documented dependency/test environment. Inspect upstream dependencies before installing them. Docker is needed only for the isolated Python/browser features; missing Docker or images must fail closed, never fall back to host execution. QQ credentials and platform permissions are operator-provisioned, not included here.

The following commands are the parent-maintained `scripts/prepare_host.py` interface. **Do not treat this documentation as evidence the script has passed tests:** check [VERIFICATION](VERIFICATION.md) and run `--help` against the actual candidate first. Paths below are operator-selected placeholders, not deployment defaults.

```bash
# Run from this suite's root; choose a disposable checkout, never the live host.
python scripts/prepare_host.py --help
python scripts/prepare_host.py --host /path/to/disposable-hermes

# Only after reviewing the patch/check output and exact pinned, clean Git base:
python scripts/prepare_host.py --host /path/to/disposable-hermes --apply

# Alternatively apply and copy the reviewed plugin in one explicit operation.
# /path/to/review-profile/plugins/guest-workspace must NOT already exist.
python scripts/prepare_host.py --host /path/to/disposable-hermes --apply \
  --install-plugin --plugin-dir /path/to/review-profile/plugins/guest-workspace
```

On Windows use quoted native forward-slash paths such as `D:/review/hermes` and `D:/review/profile/plugins/guest-workspace`. `--host` names source, while `--plugin-dir` is the complete new plugin destination, not the parent plugins directory.

Contract: no `--apply` means check/dry-run only, no writes. Applying requires a clean exact-pinned Git checkout. `--install-plugin` requires both `--apply` and explicit `--plugin-dir`; an existing plugin destination must be refused, not overwritten. The script must not alter live configuration, enable the plugin, install a service, or restart a gateway. If actual help/behavior differs, stop rather than improvise a live install. Do not run apply twice on an already patched tree as if it were the pinned clean base.

## Independent manual path

After manually reviewing the manifest and patch, you may apply the reviewed patch to the disposable exact-base checkout using Git's check/apply workflow, then validate it there. Never recursively copy a historical host tree over upstream. Plugin installation is a separate file operation: review `plugins/guest-workspace`, validate it with the prepared host's `hermes plugins validate PATH` (and inspect any private-core-import warnings), then copy it into an **absent** `guest-workspace` directory under your deliberately chosen profile's plugins directory. Do not copy another profile's configuration, memory, cache, receipts, wheels, proof files or credentials. Validation may require the overlay's private core modules; it does not establish standalone compatibility.

## Configuration and activation

- Merge reviewed keys from `examples/config.safe.yaml` into an isolated profile manually; it is not a full replacement config. Leave platform/plugin disabled during preparation.
- The example keeps `role_policy.enabled: true` with an empty public-tool allowlist, even while the platform is disabled. Never turn off this actor gate to make a tool work. Review each grant before accepting public traffic.
- Plugin loading uses the real `plugins.enabled` list: add `guest-workspace` only after validation. There is no `plugins.entries.guest-workspace.enabled` activation switch. If selected, add `guest_workspace` and/or `guest_browser` to `platform_toolsets.qqbot` **and** `platforms.qqbot.extra.role_policy.public_tools`; do not replace existing lists blindly. Browser additionally needs `browser_enabled: true`. No source patch or these scripts auto-enables any of them.
- Administrator lists are empty. Fill only complete canonical IDs confirmed from trusted transport, in the bot-owning scope. `allow_admin_from` is private/DM authorization; `group_allow_admin_from` is group authorization. They are not interchangeable. Home-channel delivery is not authorization.
- A second DM administrator in the same profile can gain access to shared MEMORY/USER despite separate chat histories. Obtain informed consent or use a separately provisioned profile; do not assume DM history isolation protects profile memory.
- Provision Docker images using [BUILD](BUILD.md); set only independently verified full image IDs. Browser requires `browser_enabled: true` as well as an image. The Python entry uses `docker_image`; no invented `docker_enabled` switch is provided.
- Keep group auto-reply opt-in and group-specific; notify participants of text/image processing and model costs first. Review permissions for QQ intents, uploads, messages, recall, and platform quotas independently.
- Validate and test in an isolated profile/test group, inspect loaded paths and effective permissions. Only then deliberately enable plugin/platform and have the operator manually reload the gateway in an external terminal. This project performs no automatic restart/service setup.

Back up any real deployment before a later manual migration. Roll back only reviewed source/plugin increments and the exact config leaves changed; never restore a stale whole-profile config over newer administrator grants. Test outcomes, installed files, process loading, runtime execution and actual QQ delivery are separate acceptance stages.
