# OrderDraft: project context (for a new chat)

> Upload/paste this doc in a new chat and say "continue OrderDraft". Full technical README: `claude/orderdraft-saas.md`.

## Product
- AI order capture SaaS for **Australian wholesalers/distributors** (food & bev, hospitality suppliers first).
- Client uploads inventory + customers (CSV/XLSX with column mapping, or Xero sync).
- Orders come in as forwarded emails (Postmark), voice notes (Deepgram), PDFs/photos (Claude vision) or pasted chat/SMS. Claude extracts them, the matcher maps them to SKUs and customers, and a **draft order** is created. Staff review and approve it.
- Staff can also enter orders and invoices **manually** (phone/counter orders, delivery fees, services).
- Approved orders go out to: the client's accounting software - Xero / QuickBooks Online / MYOB (invoice, quote, sales order) / OrderDraft's own tax invoice (PDF+GST+email) / custom API (field mapping) / webhook / CSV.
- Each client also gets a **customer ordering portal** (PWA) where their customers log in and order.
- Independent SaaS, sold as a **monthly subscription per client** (Stripe, AUD).

## Decisions taken
| Topic | Decision |
|---|---|
| Stack | FastAPI + SQLAlchemy 2 + Alembic + MySQL (SQLite local) · React 19 TS + Vite + Tailwind v4 + shadcn-style + TanStack Query |
| AI | Claude (extraction + unsure-line picking), Deepgram nova-3 `en-AU` |
| Email intake | Postmark inbound webhook, tenant address `<slug>@INBOUND_DOMAIN` |
| Matching | No pure RAG. Alias (customer-specific learned) → SKU → exact → fuzzy + order-history boost → Claude picks from top 5 |
| Invoices | **Both:** own invoices (PDF, GST, payments) + tracking of Xero invoice status |
| Manual entry | Manual orders (New order → Manual entry) + manual invoices (Invoices → New invoice, edit while draft). Manual orders skip the AI, **don't count toward the plan order limit**, but need an active subscription |
| Accounts & Tax | Per-tenant tables `account_codes` + `tax_rates` (defaults 200 Sales, 260 Other Revenue, OUTPUT 10%, EXEMPTOUTPUT 0%, BASEXCLUDED 0%). Import from Xero (button + every sync) or create manually. Codes = Xero TaxType. Tax calculated per invoice line (line code → product tax_type → GST switch) |
| Tenancy | **Har client ka apna database** (`od_t_<id>`). Main DB = tenants registry, users/login, plans, invites, usage events, integration_links. Client DB = catalog, orders, invoices, connections (tokens), tenant_settings, accounts/tax, logs. Session `bind_tenant` + before_flush guard |
| Accounting software | Xero + QuickBooks Online + MYOB behind `connectors/` contract. Har client apna account connect karta hai (platform ka ek OAuth app per software). **Ek client = ek software**; ek org/company file sirf ek client se link. Org/company-file picker. Destination type `accounting` (connected software pe jaata hai) |
| Super admin | Yes: tenants, plans, AI cost, impersonate, suspend, announcements |
| Jobs | `JOBS_MODE=thread` local/Windows, `rq` + Redis on the Linux production server |
| Plans (placeholder) | Starter $149/300 orders/2 users · Growth $349/1000/5 · Pro $799/3000/15. 14-day trial, hard limit 120% |
| Homepage | Public landing at `/` (dark premium hero with product mock, features bento, ROI calculator, live pricing from `/public/plans`, FAQ). Logged in = dashboard |
| User-facing text | **English only** for every UI string and API error (Australian users). Code comments/docstrings stay Hinglish for the dev team |

## Current status (25 Oct 2026)
- Code: `orderdraft-saas.zip` (backend + frontend + homepage). The old MVP is in `orderdraft-mvp.zip` (separate, no longer used).
- Backend: **215** pytest tests pass on SQLite; new tests also pass on MySQL/MariaDB. Migrations: `alembic/main` (0001, 0002) + `alembic/tenant` (0001 … 0010), auto-run on startup for main + every client DB (`AUTO_MIGRATE`).
- Playwright E2E scripts per round (scratch), latest covers category export, delivery-days settings + checkout dropdown, reject-with-reason → portal bell, supplier Trash.
- ⚠️ **Not yet tested with real API keys**: Claude, Deepgram, Xero, QuickBooks, MYOB, Stripe, Postmark, Web Push are all mocked in the tests (HTTP fakes). First real test: QuickBooks sandbox + Xero demo company + MYOB sandbox file.
- Ritesh is doing local testing on Windows (PowerShell: `venv\Scripts\Activate.ps1`).

## Known issues / TODO
1. ~~Export duplicate on retry~~ → fixed in Round 13 (idempotency key, see below).
2. QuickBooks production needs Intuit app review; MYOB/Xero scopes changed recently - match `MYOB_SCOPES` / `XERO_SCOPES` to your app.
3. `XERO_SCOPES` must be verified against Ritesh's Xero app (accounting settings read scope is needed for Accounts/TaxRates). Xero connection limits, certification and API pricing must be checked before a commercial launch.
4. Plan prices/limits are placeholders. Change them in Super admin → Plans and add the Stripe `price_...` ids.
5. On the homepage, `CONTACT_EMAIL` (`pages/Landing.tsx`) is a placeholder `hello@orderdraft.com.au`. Final brand name/domain not decided.
6. Manual invoices are OrderDraft (internal) invoices only - pushing a manual invoice to the accounting software is not built yet. Credit notes / consolidated weekly invoices (QuickB2B features) not ported yet.
7. Old QuickB2B `QuickBooksController` has hardcoded client ids/secrets - rotate those Intuit app secrets.
8. Server: set VAPID keys (`python -m app.cli vapid-keys`) + https for push/PWA.
9. A few config errors still name the env var (e.g. "Billing isn't set up yet (STRIPE_SECRET_KEY)") - could become "please contact support".
10. Pending feature list (Oct 2026): Stripe card payment on portal, favourites/quick order list, multiple delivery addresses, stock availability/reservation, category images + category sync from accounting, supplier bills to accounting, returns & stocktake, multi-location/POS, Twilio SMS/voicemail, Unleashed/Cin7, auto-approve above threshold, embeddings for 5k+ SKUs, S3/R2 storage, legal pages.

## Decisions (Oct 2026 - Xero fields, photos, customer email)
- Customer + Product now hold the full Xero Contact / Item fields (tenant migration `0002`). Xero POBOX address = `postal_*`, STREET = `delivery_*`; display `address` is built from delivery (else postal) when empty. QuickBooks BillAddr/ShipAddr and MYOB Location 1/2 map the same way. Partial PUTs only change sent fields (old forms don't wipe synced data).
- Product photos: OrderDraft stores them (accounting software doesn't). Max 8, 5 MB, jpg/png/webp verified with Pillow, resized 1600px + 400px thumb, `STORAGE_DIR/t<id>/products/<uuid>`, public URL `{APP_URL}/media/t<id>/products/<file>`.
- Order confirmation email on approve (Settings → Email: on/off, subject/message placeholders, CC other emails, BCC). Resend from order page.
- Sender: client's own domain via Postmark Account API (DKIM TXT + Return-Path CNAME `pm-bounces` → `pm.mtasv.net`). Until verified: `"Client via OrderDraft" <MAIL_FROM>` + Reply-To client. Free mail refused; one domain = one client. Needs `POSTMARK_ACCOUNT_TOKEN`.
- Products/customers/**suppliers** lifecycle: `deleted_at` soft delete (also sets is_active False), restore, permanent delete only from Trash and only if never used (orders/invoices; for suppliers: POs/receipts). List `?status=active|inactive|deleted|all` (`all` excludes Trash for suppliers).
- Customer pantry = `customer_prices` (customer_id + product_id unique, price nullable, starts_on/expires_on optional, note = staff only, editable in the pantry row). `catalog/pricing.py` `price_map`/`price_for`. Approve snapshots unit_price.
- Barcode: `products.barcode` (unique per tenant). Empty = internal EAN-13 `20`+id(10)+check. `/products/lookup?code=` is the scanner/POS entry point. Labels page `/products/labels?ids=`.
- Stock: `stock_movements` log, `products.reorder_level` (Low stock = qty **at or below** reorder level), `/stock` page. OrderDraft is stock master for ALL tracked products incl. Xero/QBO/MYOB. Approve order → `deduct_for_order` (setting `stock_deduct_on_approve`).
- Purchasing BUILT (migration `0006`): Supplier, SupplierProduct, PurchaseOrder/Line, StockReceipt/Line, `app/modules/purchasing/`. Negative stock allowed; weighted average cost. Product purchase tax = dropdown of purchase tax rates (free text only when no rates synced).
- Roadmap order: supplier bills → returns/stocktake → multi-location/POS. Plan doc: https://claude.ai/artifact/ArHi23h8BPQbhjJpvCqbHU
- Sorting: `app/core/sorting.py apply_sort`; frontend `useSort` + `SortTH`.
- Supplier import: CSV + Xero/QBO/MYOB. PO uses supplier link: cost, min order qty, lead time → expected date.
- Searchable pickers: `frontend/src/components/ui/search-select.tsx` — use for every product/customer/supplier dropdown.
- Ctrl+K command palette; contextual Help drawer; Help Center `/help` with articles in `frontend/src/help/articles.ts`. New page/feature = add/update article.
- Audit trail: `backend/app/core/audit.py` (`no_audit(db)` + `db.flush()` inside for bulk). Activity API `modules/reports/activity.py`. Request log `core/request_log.py` → `logs/app.log`.
- Unicode: `core/text.py`, `core/pdf_fonts.py`.

- Round 9: categories (`catalog/categories.py`, 2 levels), product page `/products/:id`, portal logins + welcome email (`modules/portal/`), super-admin announcements, sales reports (`reports/sales.py`). Migrations tenant 0007, main 0002.
- Round 10: customer portal (`portal/admin.py`, `shop.py`, `auth.py`; TenantSettings `portal_*`; PortalBanner; DraftOrder.payment_method/portal_login_id, source='portal'). Tenant 0008. Frontend `pages/portal/` (cart in localStorage `od_cart_<slug>`), staff `pages/settings/PortalSettings.tsx`.
- Round 11: portal PWA, Web Push (`core/webpush.py`), customer notifications (`portal/notify.py`), branding, staff Login as + Set password, central Customer logins list. PUT /settings/portal is PARTIAL. Sub-tabs ?s=setup|look|logins|notify|banners. Tenant 0009.
- Round 12: category import (Import data → Categories; `imports/service.py import_categories`, separators `>` / `›` only).
- Round 13 (tenant migration 0010 `20261101_0010_delivery_days_order_notify.py`):
  - **Portal order updates**: `portal/notify.py notify_order_status` called from orders router approve/reject. Bell + push to customer's logins, email to the ordering login (skipped on approve when order confirmation email already goes to the same address). Reject body `{reason}` (optional) → in notification + activity log. `portal_notifications.kind` = manual | order (order ones hidden from staff list). Staff reject dialog asks reason for portal orders.
  - **Delivery days / cut-off**: `portal/delivery.py`; TenantSettings `portal_delivery_days` ("0,2,4", 0=Mon), `portal_cutoff_time` ("16:00", tenant tz), `portal_lead_days` (default 1), `portal_closed_dates` (JSON). Rules active when days or cut-off set → delivery date required + validated. `GET /portal/{slug}/delivery-dates`. Checkout shows dropdown of 30 valid dates.
  - **Export idempotency**: `destinations/service.py idempotency_key(job)` = `orderdraft-<tenant>-<order>-<job>`, passed as `config["_idempotency_key"]` + `config["_retry"]`. Xero `Idempotency-Key` header, QBO `requestid` param, webhook/custom API header, MYOB looks up `OD<id>` by Number on retry.
  - **Category export**: `GET /categories/export.csv` (same columns as import).
  - **Supplier Trash** (`suppliers.deleted_at`): DELETE = Trash, `/restore`, `/permanent`.
  - All Hinglish user-facing errors (backend ~200 strings + frontend form errors) translated to English.

## Key files (quick reference)
- `backend/app/modules/parsing/matcher.py`: matching core · `pipeline.py`: message → draft order
- `backend/app/modules/orders/service.py`: approve (learn aliases + history + queue exports), `create_manual_order`, `reject_order(reason)`
- `backend/app/core/database.py`: MainBase/TenantBase, bind_tenant, tenant_session, provisioning, flush guard
- `backend/app/modules/connectors/`: base.py, api.py, xero.py, quickbooks.py, myob.py, service.py, sync.py, router.py
- `backend/app/modules/destinations/`: `registry.py` + one file per destination, `service.py` (jobs, retry, idempotency)
- `backend/app/modules/imports/fields.py`: import fields + synonyms
- `backend/app/modules/catalog/`: images.py, lifecycle.py, customer_detail.py, pricing.py, barcodes.py, stock.py, categories.py
- `backend/app/modules/portal/`: admin.py, shop.py, auth.py, accounts.py, notify.py, pwa.py, delivery.py, service.py
- `backend/app/modules/email/`, `core/mailer.py` (`sender_for`), `orders/confirmation.py`, `billing/limits.py`, `accounting/`, `invoices/service.py`
- Frontend: `lib/types.ts`, `pages/orders/OrderReview.tsx`, `pages/Landing.tsx`, `pages/catalog/*`, `pages/purchasing/*`, `pages/portal/*`, `pages/settings/PortalSettings.tsx`, `components/common/*`, `help/articles.ts`

## Testing quick notes
- Super admin: `.env` → `SUPERADMIN_EMAILS=you@x.com`, or `python -m app.cli make-superadmin you@x.com`. Use 2 browsers (admin normal, client incognito).
- Samples: `backend/sample_data/` (products, customers, suppliers, categories, history CSV + postmark_sample.json).
- Tests: `timeout 500 python -m pytest -q -p no:cacheprovider`; MySQL via `TEST_DATABASE_URL` + `TEST_TENANT_DATABASE_URL`.

## How Ritesh likes to work
- Hinglish chat, direct and code-first, full copy-paste-ready files, readable junior-friendly code, step-by-step with testing in between.
- When changing code: only the requested change, don't alter existing logic.