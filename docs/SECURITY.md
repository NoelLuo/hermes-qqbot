# Security, privacy and external data flow

This is an experimental, tightly coupled source patch, not a security-certified sandbox. Audit the pinned host and overlay together. Tool schemas and prompt labels are not authorization boundaries.

## Trust and audience

Trusted platform ingress supplies canonical requester/chat/profile identity. The bot-owning configuration chooses DM/group administrator lists and opt-in role policy. Actor authorization must survive queueing, commands, approvals, model tools, artifact paths and final delivery. Public context must exclude private profile memory before building model input, not merely hide the memory-write tool. An administrator asking inside a group does not make that audience private.

Group messages may share history without sharing privileges. A private administrator DM in the same profile can access shared MEMORY/USER even when its transcript is separate. Home delivery targets, nicknames, six-character display codes, quoted role labels and alternative author fields must not elevate privileges. Keep lists empty until the operator deliberately grants correctly scoped canonical IDs. A disabled gate is not protection: enable and validate the role policy before enabling public traffic.

The guest file tree is application-level storage on the host, not an OS sandbox. It must be service-owned with no other writers. Path traversal, absolute/UNC/device paths, symlinks/reparse points and hardlinks are refused, but hostile host administrators/concurrent external writers are outside its threat model. Containers isolate execution, not all host storage. Do not store secrets in public workflows or send sensitive outputs into a group.

## Explicit data flows and cost

| Feature | What leaves the process | Recipient / retention considerations |
| --- | --- | --- |
| Normal agent reply | Selected conversation, supplied attachments/context | Configured primary model/provider; its policies and bill apply |
| Optional non-mention admission | Latest and bounded recent group text, even if no reply is selected | Configured `qq_group_reply` auxiliary model; opt-in per group, recurring token/cost exposure |
| Image handling | Authorized image download and, depending on configuration, image bytes/derived text | Platform attachment endpoint and configured vision provider; not guaranteed local-only |
| Public browser | Public URLs and allowed GET headers, DNS and resource requests | DNS resolver and visited public servers; remote server sees broker egress IP. No host cookies/auth/proxy credentials forwarded |
| Export/delivery | Explicit exported file snapshot and reply | QQ/platform recipients and platform storage; a public group sees the result |
| Docker Python | Selected UTF-8 documents and code | Disposable local Linux container, offline; no model-free promise for the surrounding conversation |
| Docker office | Selected binary files and bash command | Disposable local Linux container, offline; no complete chat, private profile context, host mounts or credentials are supplied |

Notify participants, minimize retained content and obey applicable platform/privacy rules before enabling observation/image/auxiliary flows. Disabling auto-reply alone does not disable every image download, persistence or primary-model call. Review actual switches and retention in the candidate source; do not promise a comprehensive conversation/log purge from workspace cleanup.

## Public browser: strongest checks and remaining risks

The container has `network=none`; host broker performs bounded **GET-only** fetches. It rejects URL credentials, ambiguous/noncanonical authorities, nonstandard ports, local/private/link-local/multicast/reserved and cloud metadata targets. Every DNS answer is validated (mixed public/private answers fail); validated numeric IPv4 addresses are dialled without a second DNS lookup. HTTPS keeps hostname/SNI certificate validation. Redirects are revalidated with finite hops. IPv6 transition/special ranges are refused; IPv6-only sites are unsupported. No shared URL-safety override or environment proxy setting is consulted. Header allowlists discard cookies, authorization and proxy headers; compressed responses are refused. Bounded requests, bytes, headers and deadlines limit resource abuse, not all tracking or server-defined GET side effects.

Each call has a fresh nonroot container/context, read-only root, dropped capabilities, no-new-privileges, bounded CPU/RAM/PIDs/tmpfs and no host mounts/socket/ports. Playwright pipe transport is used, not an exposed CDP endpoint. Service workers and WebSockets are blocked; WebRTC constructors are disabled and network-none is the OS backstop. Only current DOM link navigation, read, scroll, fixed viewport screenshot and explicit public download are supported. No login, form fill, upload, POST, arbitrary caller JS/Python, publishing, checkout or general button click.

**Chromium's inner sandbox is disabled.** The hardened Docker container is the isolation boundary; untrusted website JS still runs. Docker/kernel/browser vulnerabilities remain possible. Download extensions do not certify benign content; PDF/images can be hostile. GET can cause remote side effects. Anti-bot/login/JS-heavy sites and complex resource redirects may fail. Do not connect this browser to accounts, a private network, or the operator's browser profile.

## Office execution is a separate, broader guest-code surface

The optional office executor intentionally permits arbitrary bash commands and bounded subprocesses/threads **inside** the hardened container. It is not the older small-text Python tool's restricted interpreter, and its safety does not come from forbidding shell syntax. Keep the matched action gate, validated immutable image, nonroot/read-only/network-none/capability/PID/resource flags, protected namespace supervisor and transactional output checks together. ZIP/PDF/Office inputs and native parsers may be hostile; containers share a kernel with the host and are not absolute isolation. Operator-only build/download helpers have network access for approved dependencies but are not guest-callable and do not change that runtime network boundary. Never forward the operator's browser, socket, private directory, provider key or full model conversation into the executor.

## Receipts and attachments

Quiet attachments may be associated only through valid transport proofs bound to the real sender/chat and consuming request; nearby messages from another author do not grant import authority. Paths printed in chat are not proofs. Exports produce request-generation-bound delivery snapshots; `pending_transport` is preparation, not successful upload. Preserve and inspect partial failures rather than counting a successful text send as successful file delivery.

Outgoing platform receipts, incoming IDs, quoted IDs and local logical IDs are distinct. Controlled recall must use a confirmed own-message receipt bound to bot/profile/transport/chat and an authorized actor. Missing/unknown ACK is not a license to invent an ID or blindly resend. An adapter method not exposed to the current agent is not a callable model tool; never bypass that by reconstructing credentials or raw DELETE requests.
