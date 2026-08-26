# Tool inventory

The Tools route displays the configurable Hermes toolsets discovered from the Hermes installation.

The telemetry sidecar resolves the installation through `HERMES_HOME` and loads `hermes-agent/tools_config.py`. It does not assume that the Hermes checkout is a sibling of the Mission Control repository. This keeps the inventory accurate for local installations and worktrees.

The tool inventory is read-only in Mission Control. Tool availability and required credentials continue to be managed by the Hermes installation and its configuration.
