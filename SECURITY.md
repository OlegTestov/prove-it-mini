# Security

## What the plugin can do

Prove-It Mini runs the test command you set in `.claude/prove-it/config.json` when the agent tries to finish after changing code. It makes no network requests and sends no telemetry. It writes only to `.prove-it/` in your repository (state and gate log), plus what setup and teardown change (listed in the README).

## Reporting a vulnerability

Please report security issues privately to os.testov@gmail.com, or through GitHub's "Report a vulnerability" on this repository. Include steps to reproduce and the version (see `.claude-plugin/plugin.json`). You will get a reply within 7 days.

## Supported versions

Only the latest release on `main` gets fixes.
