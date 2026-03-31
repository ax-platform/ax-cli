# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in ax-cli, please report it responsibly:

1. **Do NOT open a public GitHub issue**
2. Email **security@paxai.app** with details
3. Include steps to reproduce if possible
4. We will acknowledge within 48 hours and provide a fix timeline

## Scope

ax-cli handles authentication tokens (PATs) and communicates with the aX Platform API. Security concerns include:

- Token storage and handling (`~/.ax/config.toml`)
- API communication (HTTPS enforcement)
- Command injection via user input
- Credential leakage in logs or error messages

## Token Safety

- Tokens are stored in `~/.ax/config.toml` with `0600` permissions
- Tokens are never logged or printed in full (masked in `ax auth token show`)
- The `.ax/` directory is in `.gitignore` to prevent accidental commits
