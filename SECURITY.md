Credentials belong in local ignored files or hosting environment variables. Never
commit API keys, account passwords, dashboard passwords, or private wallet data.

Install the repository's hooks with `pre-commit install`. The Gitleaks hook checks
staged changes and redacts its output. Before publishing, scan all reachable Git
history with `gitleaks git --redact --log-opts=--all .`.

Credential-shaped values in inherited security tests are synthetic. Build such
fixtures from explicit test components rather than copying a real credential.
Removing a credential from Git does not revoke it: replace any actual exposed
credential at its provider and update the private hosting configuration. GitHub
cached commit pages and other people's clones can outlive a history rewrite.

The October 2026 cleanup replaced realistic test literals throughout the main
branch history. Existing clones must be freshly cloned or reset to the rewritten
main branch before pushing, so old history is not restored.
