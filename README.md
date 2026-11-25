# Abschlusspruefer DE: read-only MCP integrations

This repository contains two separate Docker services:

- `getmyinvoices-mcp` exposes read-only document discovery, metadata and protected downloads.
- `lexware-office-mcp` exposes documented read-only organization, voucher, contact, invoice, credit-note and recurring-template operations.

- `lexware_buchhalter` (no Docker, runs on the Windows PC with Lexware buchhaltung desktop) reads the local Lexware SQLite files read-only: companies, chart of accounts, journal, Buchungsstapel, balances and any mapped table.

No create, update, delete, upload, booking, finalization or submission tool is registered. Provider requests use `GET` exclusively.

## Security model

- Provider credentials are read from environment variables and never returned by tools.
- Each MCP endpoint requires `Authorization: Bearer <MCP_SERVER_AUTH>`.
- GetMyInvoices binds the account to the API key. Before the first data access the service calls `GET /account` and refuses to work unless `accountId` equals `GMI_ACCOUNT_SCOPE` and `apiKeyType` is `READ_ONLY`.
- Lexware validates `LEXWARE_ORGANIZATION_SCOPE` against `GET /v1/profile` and rejects mismatching resource responses.
- IDs cannot contain path separators; date ranges and page sizes are bounded.
- Provider calls use timeouts and bounded retries for `429` and transient `5xx` responses.
- Redirects never leave HTTPS, and credential headers (`Authorization`, `X-API-Key`) are stripped when a redirect leaves the provider origin.
- Each container receives only its own provider token; `.env` is used for compose interpolation, not passed wholesale via `env_file`.
- Downloads accept only PDF, XML, JPEG and PNG, enforce a streaming byte limit, use mode `0600`, receive a SHA-256 checksum and expire from temporary storage.
- Docker containers run as an unprivileged user with a read-only root filesystem, all Linux capabilities dropped and only a small tmpfs for ephemeral downloads.
- Logs contain status information only. Tokens, query strings, response bodies and document data are not logged.

## Configuration

Copy `.env.example` to a local `.env` and replace every placeholder. Do not commit `.env`.

GetMyInvoices uses the Accounts API v3 (`https://api.getmyinvoices.com/accounts/v3`, header `X-API-KEY`, GET only):

| Tool | Endpoint |
|---|---|
| `gmi_health_check` | `GET /account` (account id and READ_ONLY key type) |
| `gmi_list_documents` | `GET /documents` with `startDateFilter`, `endDateFilter`, `perPage`, `pageNumber`, `documentTypeFilter[]`, `paymentStatusFilter`, `companyFilter` |
| `gmi_get_document_metadata` | `GET /documents/{documentUid}` |
| `gmi_download_document` | `GET /documents/{documentUid}`, file taken from the JSON field `fileContent` |
| `gmi_list_document_types` | static `documentTypeFilter` enum (no provider endpoint) |

Still **unconfirmed** against the official documentation (the published spec could only be read through a mirror): the query parameter that switches the file content of a single document on or off (`GMI_DETAIL_QUERY_JSON` / `GMI_DOWNLOAD_QUERY_JSON`), the base64 encoding of `fileContent`, and the field names for amounts, dates and supplier in document records. Responses carry `field_mapping_verified: false` until this is checked with a real account.

Lexware defaults to `https://api.lexware.io` and uses Bearer authentication. The implementation relies only on GET operations documented as of 2026-09-17:

- `/v1/profile`
- `/v1/voucherlist`
- type-specific voucher detail endpoints
- `/v1/contacts` and `/v1/contacts/{id}`
- type-specific `/file` endpoints and `/v1/files/{id}` for bookkeeping vouchers
- `/v1/recurring-templates`

The official payments API documents lookup by one voucher ID, not a date-filtered payment collection. Therefore `lexware_list_payments` is not exposed; `lexware_get_api_capabilities` reports this explicitly.

## Run

```bash
docker compose up --build
```

The MCP endpoints are available at `http://localhost:8081/mcp` and `http://localhost:8082/mcp`; the ports are bound to `127.0.0.1` only. Liveness endpoints are `/healthz`. In production, publish both services behind separate HTTPS endpoints, set `MCP_ALLOWED_HOSTS` to their public hostnames and set `GMI_PUBLIC_BASE_URL` / `LEXWARE_PUBLIC_BASE_URL` if protected download URLs should be returned.

The `/downloads/{reference}` route uses the same Bearer token as its MCP service. References expire after `DOWNLOAD_TTL_SECONDS`; provider documents remain unchanged.

## Lexware buchhaltung desktop (on-premise)

Lexware buchhaltung (plus) 2027 keeps each company in `C:\ProgramData\Lexware\buchhalter\Daten\0000000N\` as SQLite files: `BHStdAccountList.dbs` (chart of accounts) and `YYYY\BHStdYear.dbs` per fiscal year. Most fields are packed into BLOB columns; the service decodes them generically from Lexware's own `__mapping` table, and TEXT columns from cp1252. Files are opened with `mode=ro` and `PRAGMA query_only`, so the service cannot write.

```powershell
$env:LXBH_DATA_DIR = 'C:\ProgramData\Lexware\buchhalter\Daten'   # default
$env:MCP_SERVER_AUTH = '<long random token>'
python -m services.lexware_buchhalter.server
```

Tools: `lxbh_list_companies`, `lxbh_list_accounts`, `lxbh_list_bookings` (journal and/or Stapel, date and account filter), `lxbh_account_balances`, `lxbh_list_tables`, `lxbh_read_table`. The service never writes into the Lexware database. For new bookings, `lxbh_create_datev_import` validates them against the chart of accounts (existing, not locked, no BU key on automatic tax accounts, date inside the fiscal year, DATEV character set) and returns a preview; only with `confirm: true` it writes a DATEV-Format file (EXTF 700, Buchungsstapel, format version 13, Windows-1252) into `LXBH_EXPORT_DIR\<company>`. The user imports that file in Lexware (Datei > Import > DATEV-Import); the bookings then wait under "Import Buchungsdaten" until they are checked and booked into the Stapel. The official local "Lexware API Service" would allow direct bookings but needs a client key issued by Lexware.

## Use as a claude.ai connector (OAuth)

claude.ai custom connectors cannot send a fixed `Authorization` header, so each service can act as its own minimal OAuth 2.1 authorization server for one owner:

1. Publish the service under HTTPS (for example a Cloudflare Tunnel to `localhost:8082`).
2. Set `MCP_PUBLIC_BASE_URL` (e.g. `https://lexware-mcp.example.de`), a random `OAUTH_SIGNING_KEY` (32+ characters) and `OAUTH_LOGIN_PASSWORD` (12+ characters). Add the hostname to `MCP_ALLOWED_HOSTS`.
3. In claude.ai, add a custom connector with the URL `https://<host>/mcp`. Claude registers itself, opens a password page on the service, and receives tokens after the correct password.

Details: dynamic client registration accepts only Claude callback URLs, PKCE S256 is mandatory, codes are single-use and valid for 5 minutes, access tokens for 1 hour, refresh tokens for 30 days. Client IDs and tokens are HMAC-signed and stateless, so restarts do not log Claude out; changing `OAUTH_SIGNING_KEY` revokes all of them. Five wrong passwords lock the page for 15 minutes. The static `MCP_SERVER_AUTH` token keeps working for local tests.

## Test

```bash
python -m compileall -q common services tests
python -m unittest discover -s tests -v
docker compose config
docker build -f services/getmyinvoices/Dockerfile -t getmyinvoices-mcp:test .
docker build -f services/lexware/Dockerfile -t lexware-office-mcp:test .
```

Tests use synthetic data only. No real credentials, client data or documents belong in this repository.

## MCP transport

Both services implement stateless Streamable HTTP JSON responses for MCP protocol version `2025-03-26`, including `initialize`, `ping`, `tools/list`, `tools/call` and initialized notifications. They intentionally do not implement provider write methods or MCP prompts that could broaden the data-access surface.

## Source references

- GetMyInvoices API v3 documentation: <https://api.getmyinvoices.com/accounts/v3/doc/index.html>
- Lexware API documentation: <https://developers.lexware.io/>
- Model Context Protocol TypeScript SDK server guidance used to validate the remote Streamable HTTP shape: <https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/server.md>

