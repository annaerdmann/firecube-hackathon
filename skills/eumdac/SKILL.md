---
name: eumdac
description: Use when working with EUMDAC, the EUMETSAT Data Access Client, for EUMETSAT Data Store, Data Tailor, authentication, discovery, product search, downloads, subscriptions, livefeed, orders, or Python/CLI examples.
---

# EUMDAC

Use this skill for EUMDAC user workflows: installing the client, authenticating, discovering collections/products, searching EUMETSAT Data Store, downloading products, using Data Tailor, managing subscriptions/live feeds, handling orders, or writing Python/CLI examples.

This skill is grounded in `eumdac 3.1.1` and the EUMDAC v3.1.0 API documentation, and is verified against that release only. Install commands pin `eumdac==3.1.1` (see [install.md](references/install.md)). Prefer the installed `eumdac --help` output and official EUMETSAT docs when exact flags, collection behavior, or service limits matter.

## Operating Rules

1. Never print, commit, or log EUMETSAT consumer keys, consumer secrets, bearer tokens, subscription credentials, account details, or private downloaded data paths.
2. Do not run `eumdac set-credentials` or `eumdac subscribe set-credentials` if an existing credentials file is present unless the user explicitly wants to replace it.
3. Treat Data Store, Data Tailor, subscription, livefeed, and order commands as networked service calls. Confirm credentials, network access, and collection/product IDs before running them.
4. If credentials are missing, provide commands or code with placeholders instead of inventing values.
5. Use credential-free checks for validation unless the user explicitly provides credentials and asks for live service access.
6. For exact command flags, run `eumdac <command> --help` locally before finalizing examples.
7. Before any download: state the product count and expected size (`Product.size` in the Python API gives bytes before downloading; otherwise measure one product first), ask for the destination directory, check free space there with `df -h`, and get authorisation once with those numbers. Print one progress line before a download or long search and one result line after; never leave a live call running silently.
8. Locate the CLI with `command -v eumdac` before anything else. Never assume a project `.venv`, a personal virtualenv directory, or a repository-specific path; install per [install.md](references/install.md) into standard user-tool locations or an environment the user names.
9. Run every live Data Store, Data Tailor, subscription, or order call under a shell timeout (for example `timeout 120 eumdac search ...`) and keep `--limit` on searches, so a slow or hanging service call cannot block the session. See [datastore-filtering.md](references/datastore-filtering.md) for the known `--sort` plus `--bbox` hang.
10. Read-only commands run without confirmation: `describe`, `search`, `token` validity checks, `subscribe list`, `tailor list`/`status`/`log`/`quota`/`resources`, `local-tailor instances`/`show`, and `order list`/`status`. State-changing, long-running, or expensive commands need explicit user confirmation, after stating what will happen: `download`, `download-metalink`, `subscribe add`/`download`, `livefeed`, `tailor post`/`download`/`delete`/`cancel`/`clean`, `local-tailor set`/`remove`, `order resume`/`restart`/`delete`/`housekeep`, `set-credentials` and `subscribe set-credentials` (each overwrites stored credentials), and the Python equivalents: bulk `product.open()` loops and `DataTailor.new_customisation`/`new_customisations`.

## Response Contract

When the answer proposes commands, code, or execution, include:

1. **Workflow type**: CLI, Python API, or both.
2. **Prerequisites**: installed EUMDAC, credentials, network access, collection/product IDs, and whether live services are required.
3. **Credential safety**: placeholders for secrets and no token/key echoing.
4. **Command or code**: minimal examples verified against installed help/API, or clearly marked as illustrative.
5. **Validation status**: what was run locally, what was not run, and why.

For short factual questions, answer directly without the template.

## Workflow

1. Capture execution context: EUMDAC version, CLI vs Python API, environment manager, credentials status, network availability, and target service.
2. Identify the intent category using the routing table below. If the request spans categories, load only the matching references.
3. Inspect local help/API before giving exact flags or method signatures: `eumdac --help`, `eumdac <command> --help`, or Python introspection.
4. Propose the minimal safe command/code, including placeholders for secrets and user-specific IDs.
5. Validate with credential-free checks when possible. Do not claim live validation unless real EUMETSAT service calls were run.
6. Emit the Response Contract when commands or code are proposed.

## When To Use This Skill

Activate when:

- Installing or validating EUMDAC.
- Setting, checking, or safely handling EUMETSAT Data Store or subscription credentials.
- Discovering/listing EUMETSAT Data Store collections or products.
- Searching, filtering, downloading, or using Data Store metalink carts.
- Writing EUMDAC Python API examples using `AccessToken`, `DataStore`, `DataTailor`, collections, products, or customisations.
- Using Data Tailor, local-tailor, orders, subscriptions, or livefeed.
- Troubleshooting EUMDAC CLI/API errors, credentials, local config, or version-specific behavior.

Do not use for:

- General EUMETSAT science/product interpretation unrelated to EUMDAC commands or APIs.
- Generic Python packaging questions unless EUMDAC installation is involved.
- Data analysis or visualisation after files are already downloaded, unless the task needs EUMDAC metadata or download behavior.
- EUMETView, Data Store web UI, or Data Tailor web UI workflows unless the user is translating them to EUMDAC.
- Choosing which collection, channel, or product fits a science need; use a product-specific skill only if one is installed in the current session, then return here for EUMDAC commands.

## Reasoning Traps

| Observed | Tempting conclusion | Check first |
| --- | --- | --- |
| User names a collection, no files | Download to be helpful | Ask; download only when the user authorises it, into a directory they name |
| Search with an area filter | Add `--sort` to get the latest | Sorting pages the whole result set; narrow `--time-range`, keep `--limit`, run under `timeout` |
| Product ID held in a shell variable | Pass it straight to `download` | An empty value hangs indefinitely; check the variable is set |
| `--bbox` given to a search | Results are cropped to the box | It filters whole products; cropping is Data Tailor or local processing |
| Credentials file exists | Run `set-credentials` to be sure | Never overwrite without the user asking |
| Products are needed | Download them now | State count, size, and destination, check free space, get authorisation, run under `timeout` |
| A download or search will take a while | Report when it ends | One progress line before, one result line after |
| User has no files and asks where their data is | Search their disks | Ask where the data is, or offer a download they authorise |
| A rule applies | Explain the rule to the user | Apply it; state only what is needed in one line |

## Route By Intent

Load only the reference needed for the task:

| Intent or symptom | Read this reference | First checks |
| --- | --- | --- |
| Install EUMDAC: missing CLI, wrong Python, not on `PATH` | [install.md](references/install.md) | Python/conda/uv preference, project vs user environment |
| Set or validate credentials: `set-credentials`, token, 401/403, existing `~/.eumdac/credentials` | [auth.md](references/auth.md) | Existing credentials file, token validity, credential replacement intent |
| Discover/list/describe Data Store collections or products | [datastore-discovery.md](references/datastore-discovery.md) | EUMDAC version, collection/product ID if known |
| Search/filter Data Store products by date, publication, area, satellite, product type, orbit/cycle, filename | [datastore-filtering.md](references/datastore-filtering.md) | Collection ID, time range, spatial/product/orbit filters |
| Download Data Store products: entries, integrity, coverage, threads, output layout | [datastore-download.md](references/datastore-download.md) | Product ID or search filters, output directory, storage/quota expectations |
| Another skill (for example `firecube`) needs Data Store files on disk | [datastore-download.md](references/datastore-download.md) | Run the full size/space/authorisation procedure there, then report the destination directory back so the calling workflow can continue |
| Download Data Store metalink carts (cart XML) | [datastore-metalink.md](references/datastore-metalink.md) | Metalink file path, output directory, integrity/layout preferences |
| Write Python API examples: `AccessToken`, `DataStore`, `DataTailor`, collections/products/customisations | [python-api.md](references/python-api.md) | Installed API version, credentials, collection/product IDs |
| Use Data Tailor, local-tailor, or orders: customisation jobs, chains, order status/retry/delete | [tailor-orders.md](references/tailor-orders.md) | Chain config, local vs web service, customisation/order ID |
| Use subscriptions or livefeed: `subscribe`, `subs`, `livefeed`, tags, long-running downloads | [subscriptions-livefeed.md](references/subscriptions-livefeed.md) | Subscription credentials, tag, collection filters, long-running process expectations |
| Debug EUMDAC errors: sanitized errors, debug/trace, failed imports, no results, stuck jobs | [troubleshooting.md](references/troubleshooting.md) | Exact command, sanitized error output, installed version |

## Core CLI Surface

`eumdac 3.1.1` exposes these top-level commands. Some guides describe the user workflow as "discover"; in this validated CLI version, discovery is done with `describe`.

```text
set-credentials
token
describe
search
download
download-metalink
subscribe
subs
livefeed
tailor
local-tailor
order
```

Always inspect command help before using advanced options:

```bash
eumdac --version
eumdac --help
eumdac <command> --help
```

## Core Python Surface

The main public classes are `eumdac.AccessToken`, `eumdac.DataStore`, and `eumdac.DataTailor`. See [python-api.md](references/python-api.md) for setup, search, product access, and Data Tailor examples.

Use real collection/product IDs supplied by the user or discovered through EUMETSAT docs/Data Store. Do not guess scientific product IDs unless the user asks for an illustrative placeholder.
