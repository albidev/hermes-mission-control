# Security Policy

## Scope

Mission Control is a local-first dashboard. The telemetry sidecar can expose system metrics, Hermes session metadata, logs, and selected knowledge files. Treat it as a trusted-network application, not as a public internet service.

## Supported versions

Only the latest commit on `main` is supported for security fixes.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Report it privately through GitHub's **Report a vulnerability** flow, or contact the repository maintainer through the private contact method shown on the GitHub profile.

Include:

- affected version or commit;
- reproducible steps or a minimal proof of concept;
- impact and expected severity;
- any suggested mitigation.

You should receive an acknowledgement within seven days. Please allow time for a fix before public disclosure.

## Deployment guidance

- Never commit `.env` or bearer tokens.
- Bind the telemetry server only to trusted interfaces or protect it behind a private network such as Tailscale.
- Use a strong random `MISSION_CONTROL_TOKEN`.
- Do not expose port `8765` directly to the public internet.
- Review the knowledge-file allowlist before deploying on a shared machine.
