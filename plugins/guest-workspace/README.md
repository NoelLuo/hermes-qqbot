# Guest workspace — coupled local plugin

This plugin registers `guest_workspace`, `guest_browser` and lifecycle maintenance against the suite's **patched** Hermes host. It is experimental, unofficial, and **not standalone or universally installable**. Target host: NousResearch/hermes-agent `ab44d8acf7bca230aea8574f2f2ab4666ca293ce`. The plugin entry does not apply the required core patch.

Core dependencies include:

- `tools/guest_workspace_{interface,store,docker,lifecycle,maintenance,text}.py` (including the shared text-decoding helper).
- `tools/guest_browser_{interface,service,docker,worker,network}.py`.
- `agent/request_security.py`, `agent/request_workspace_guidance.py` and the `agent/agent_init.py` wiring.
- `gateway/{inbound_attachment_proofs,guest_export_delivery,request_security,run_turn_prepare,run_turn_runner,session_envelope,session_ingress}.py`, the platform base attachment contract, and the QQ adapter/group event/history/image/attachment integration.

This dependency overview is not an installation file manifest; review the complete suite manifest/patch. Private-core-import validation warnings reflect real coupling and must not be hidden. Upgrades require a coordinated source/plugin/image migration; copying only this directory does not restore the attachment proof, queue or final delivery paths.

See [deployment](../../docs/DEPLOYMENT.md), [limits](../../docs/CAPABILITIES.md), [privacy/security](../../docs/SECURITY.md), [runtime build](browser-runtime/README.md), and [verification](../../docs/VERIFICATION.md). Install only to an explicitly selected **absent** profile plugin destination after reviewing and validating the matched host. Default examples are disabled; no script here promises to enable a plugin, edit credentials, install a service or restart a gateway.

Persistent files are service-owned application storage, separated by profile/chat/real requester. They are not OS sandbox storage or a secrets vault. Offline Python and browser execution use fresh hardened containers with no host execution fallback. Public results still go to the public audience. Read supports UTF-8, BOM UTF-16/32 and GB18030 fallback, not certified encoding identification; Python documents remain UTF-8 only.

Browser worker source is baked into the image from the reviewed overlay. Rebuild when it changes, verify the container worker SHA256, then configure the resulting full immutable image ID. Host source edits alone do not update an existing image. Export success is pending transport, not actual QQ delivery; only explicit exports are queued for delivery. See the suite ledger for public-candidate test status.
