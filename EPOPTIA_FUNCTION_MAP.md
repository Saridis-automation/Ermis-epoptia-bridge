# Epoptia function map

Evidence: live read-only observations supplied with this task, recorded here
without making a live request. “Verified” below means observed in that supplied
record, not independently rechecked during implementation. UI visibility does
not establish API permissions, successful writes, response schemas or full coverage.

## Coverage and security matrix

| Evidence class | Coverage | Autonomous execution |
| --- | --- | --- |
| Read verified | Listed navigation/admin/quick-create routes were observed | Existing reviewed read tools only; this map grants no new access |
| Form contract verified read-only | Explicit POST routes and fields below | No live write verified |
| Feature observed; contract pending | Controls/features listed without an exact route or field contract | Disabled pending deeper mapping |
| Inferred/pending | Methods, payload encodings, responses, limits, permissions and endpoints not explicitly supplied | Never infer an executable route |
| Write not yet live-verified | All mutations, including the two new actions | Proposal/confirmation only; execution fails closed |
| Excluded | Destructive operations, billing/subscription changes, user passwords and access/permission changes | Excluded from autonomous writes |

## Navigation: observed routes

| Module | Route | Function / coverage boundary |
| --- | --- | --- |
| Dashboard | `/dashboard` | Production overview; underlying data contracts pending |
| Standby | `/standby` | Waiting production; detailed operations pending |
| Workorders | `/workorders` | Active order navigation |
| Loads | `/loads` | Load management; detail/create/update/assignment contracts pending |
| History | `/workorders/history` | Historical orders; filters/export contracts pending |
| Production reports | `/reports/factory/productiondata` | Production data reporting; query/export contracts pending |
| Warning lines | `/warninglines` | Warning/event log navigation; resolution contracts pending |
| Workstation comments | `/workstations/comments` | Comments/logs; write contracts pending |

## Administration: observed routes

| Module | Route | Functions and deeper mapping still needed |
| --- | --- | --- |
| Configuration | `/controlpanel` | Settings and configuration field contracts |
| Integrations | `/webhooks` | Webhook listing/configuration, events, delivery/retry contracts |
| Tags | `/tags` | Tag management and assignment contracts |
| Print templates | `/printings` | Template management, preview/print contracts |
| Workstations | `/workstations` | Station configuration and operational relationships |
| Terminals | `/terminals` | Terminal configuration and station association |
| Workflows | `/workflows` | Templates, sections, steps and assignment contracts |
| Custom fields | `/customfields` | Field definitions, types and value contracts |
| Custom-field groups | `/custom-fields-groups` | Group definitions, membership and scope |
| Warnings | `/warnings` | Warning definitions; distinguish from warning-line logs |
| Library | `/library` | Library content and attachment contracts |
| Clients | `/clients` | List/search and client detail/update contracts |
| Products | `/products` | Product master catalog; contracts below |
| Users | `/users` | User management; password mutations excluded |
| Permissions | `/permissions` | Role/access administration; autonomous changes excluded |
| Shifts | `/shifts` | Shift/schedule configuration contracts |
| Profile | `/profile` | Profile details; authentication/password changes excluded |
| Subscription | `/subscription` | Plan/billing visibility; billing mutations excluded |
| Version | `/version` | Version information; no mutation contract supplied |

The routes above are verified observations. Descriptions identify module scope;
individual controls, verbs and payloads remain pending unless explicitly listed below.

## Quick create

Observed form pages: `/workorders/create`, `/client/create`, `/product/create`.
Client submission endpoint and fields are pending. Product submission uses the
plural `/products/store` route; do not derive POST routes from page names.

## Products

Observed features: list, search, filter, import, export, delete; workflow from a
template; tags; BOM add/remove; comments; custom fields; files; image; active WOLs.
Import, delete and BOM removal are not authorized autonomous writes. Except for
the following rows, feature endpoints and payloads are pending.

| Verified read-only form contract | Fields | Pending verification |
| --- | --- | --- |
| `POST /products/store` | `product_type`, `name`, `is_active`, `image`, `infosupplier[]`, `infoprice[]` | Requiredness, encodings, validation, responses |
| `POST /products/update/{id}` | `name` | Authenticated write, CSRF, response and concurrency semantics |
| `POST /product/workflow/from-template` | `sectionId`, `templateId` | Other required context, responses, permissions |
| `POST /product/tags` | Not supplied | Payload and response contract |

## Production: workorders and WOLs

Observed workorder-create form fields: `client`, `productionDate`, `workorderCode`,
`workOrderComment`, `wolCode`, `product`, `workorderLineDescription`, `quantity`,
`tag`, `workorderLineComment`, `tmpWorkorderLineId`. Submission route/method,
requiredness, date encoding and multi-line behavior are pending.

Observed workorder-detail features: status, email, print, client, load, copy,
add-WOL, completion, import, delete, scrap, warehouse. The supplied observations
refer to observed endpoints but do not provide their literal paths or contracts.
Those endpoints are therefore **pending**, not invented here. Email delivery,
printing and mutations require separately reviewed authorization and contracts;
delete/scrap remain excluded from autonomous writes.

Verified read-only form contract: `POST /workorderlines/update/{id}` with field
`description`. Observed WOL-detail features: status, tags, custom fields,
completion, move, copy, ship/unship and other operational features. Their exact
routes, methods, payloads, state transitions, side effects and permissions remain
pending. “Other operational features” is not an exhaustive verified inventory.

**Critical data model distinction:** product `name` is the product master name;
WOL `description` belongs to a particular workorder line. Changing one must never
implicitly change the other. The create-form `workorderLineDescription` is also
not the product master name. IDs must come from the matching entity namespace.

## First admin-write foundation

Separately named actions exposed through `ermis_gateway_execute`:

- `update_product_name(product_id, expected_current_name, new_name)`
- `update_wol_description(wol_id, expected_current_description, new_description)`

Both are writes under the existing two-step confirmation policy. Proposal shows
exact target/expected/new arguments; request performs no read or write. Unknown
fields are rejected. IDs must be integers (not booleans) from 1 through
999999999999999. Expected/new values must be nonblank, control-free strings,
maximum 255 characters for names and 2000 for descriptions. These are local
conservative policy limits, **not verified Epoptia field limits**. No trimming or
normalization changes the expected value; concurrency comparison is exact.

The adapter holds only fixed relative POST route templates and form field names.
It loads no credentials, session or CSRF material, and has no write method.
Default confirmed execution returns `ok:false`, `status:auth_required`,
`write_performed:false`. An internal future read-only transport seam requires
session/CSRF readiness; mismatch returns `conflict`, unreadable data returns
`read_unverified`, and an exact match still returns `write_transport_unverified`.
Outcomes expose only fixed labels, action and numeric target ID, never exceptions,
raw records, session headers or field values. Proposals necessarily contain the
user-supplied business values and must not be used as audit log records.

Before enabling any transport, verify the authenticated same-origin client,
CSRF handling, no redirects, entity-specific current-value reads, request/response
contracts, and post-write verification. Read-before-write alone leaves a race
unless the server offers atomic compare-and-set/versioning; this is still pending.
No success or live write verification is claimed by this foundation.
