# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.4.x   | Yes       |
| 0.3.x   | Critical fixes only |
| < 0.2   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in ax-cli, please report it responsibly:

1. **Do NOT open a public GitHub issue**
2. Email **security@paxai.app** with details
3. Include steps to reproduce if possible
4. We will acknowledge within 48 hours and provide a fix timeline

Do not include raw PATs, JWTs, API keys, or production secrets in the initial
report. If a credential is involved, include the token prefix and last four
characters only.

## Scope

ax-cli handles authentication tokens (PATs) and communicates with the aX Platform API. Security concerns include:

- User PAT, agent PAT, and exchanged JWT handling
- Token storage and handling (`~/.ax/config.toml`, token files, and cache files)
- API communication (HTTPS enforcement)
- Command injection via user input
- Credential leakage in logs or error messages

## Token Safety

- Tokens are stored in config or token files with `0600` permissions
- Tokens are never logged or printed in full
- User PATs should be treated as bootstrap credentials, not agent runtime
  credentials
- Agent runtime workflows should use agent-bound PATs or exchanged short-lived
  JWTs
- The `.ax/` directory is in `.gitignore` to prevent accidental commits
