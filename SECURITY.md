# Security Policy

## Reporting a vulnerability

Report security vulnerabilities in Tenstorrent skills privately. This includes
the packaged skills, prompts, supporting scripts, and marketplace workflows.
Do not report vulnerabilities through public GitHub issues or pull requests.

Use GitHub's private vulnerability reporting feature when it is available:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Describe the issue, its potential impact, and the steps needed to reproduce it.

If that option is unavailable, or you need to discuss a report, contact
**ospo@tenstorrent.com**. This email route also applies while the repository has
restricted visibility.

See [GitHub's private reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-a-vulnerability/privately-reporting-a-security-vulnerability)
for more detail.

## What to include

- The affected plugin or skill and its version or commit SHA.
- The agent host and version, operating system, and relevant configuration.
- A minimal reproducer, including the prompt or input that triggers the issue.
- The observed behavior, expected behavior, and potential impact.
- Suggested fixes, if you have them.

Remove credentials, private source code, and personal information from reproducer
logs unless they are necessary to explain the issue and can be shared privately.

## Our security process

1. **Acknowledgment:** Tenstorrent will respond within **2 business days**.
2. **Triage:** Maintainers assess the impact and identify affected plugins and versions.
3. **Fix development:** Maintainers develop a fix privately, with reporter feedback
   when possible, and prepare updated plugin versions where needed.
4. **Disclosure:** A security advisory is published once the fix is available, with
   affected versions and update instructions.

For ordinary bugs and feature requests, follow [CONTRIBUTING.md](CONTRIBUTING.md).
