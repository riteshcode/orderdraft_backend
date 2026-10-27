# OrderDraft: AI order capture SaaS

A multi-tenant SaaS for Australian wholesalers and distributors. Customers' order **emails, voicemails, PDF POs and chat messages** come in, AI turns them into **draft orders**, staff review and approve them, and orders go out via **Xero / QuickBooks Online / MYOB / own tax invoice / custom API / webhook / CSV**. **Every client gets its own database**, and connects **its own** accounting account.

```
            ┌──────────── Sources ─────────────┐
            │ CSV/XLSX  ·  Xero / QuickBooks / MYOB │──► Products, Customers, Order history
            └───────────────────────────────────┘
Forwarded email (Postmark) ─┐
Voice note (Deepgram en-AU) ─┼─► Claude extraction ─► Customer + SKU matching ─► Draft order ─► Review UI
PDF / photo (Claude vision) ─┤                         alias → SKU → exact →
Chat / SMS paste ────────────┘                         fuzzy + history → Claude
                                                                                     │ Approve
                                                       learn aliases + history  ◄────┤
            ┌──────────── Destinations (ExportJob + retry) ──────────────┐           ▼
            │ Accounting software (invoice/quote/order) · OrderDraft invoice │◄──────┘
            │ (PDF + GST + email) · Custom API (field mapping) · Webhook │
            └────────────────────────────────────────────────────────────┘
                      Invoices module: draft → sent → paid (status via webhook / polling)
```

## What's included

| Area | Features |
|---|---|
| **Intake** | Postmark inbound webhook (a unique address per tenant), voice/PDF/photo upload, text paste, inbox log + retry |
| **AI** | Claude structured extraction (forwarded-email aware, AU dates), Deepgram nova-3 `en-AU` with product keyterms, scanned PDFs/photos go to Claude vision |
| **Matching** | Customer-specific learned aliases, SKU, exact, fuzzy + order-history boost, Claude picks for unsure lines, "same as last week" repeat |
| **Catalog** | Products/customers CRUD with the **full Xero Contact / Item fields** (first/last name, phone/mobile/fax/DDI, postal + delivery address, attention, ACN, bank account, sales/purchase tax type, currency, customer/supplier flags; purchase description/price/account/tax, inventory tracked + qty on hand, sold/purchased) - mapped from Xero, QuickBooks and MYOB on sync. **Product photos** (up to 8, auto resize + thumbnail, main photo, reorder) for the future customer portal. **CSV/XLSX import wizard** (auto column suggest → map → dry-run preview → import, saved mapping templates), order history import |
| **Multi-tenant** | **Per-client database** (main DB = tenants registry, logins, plans, usage, integration links). Every request/job is bound to one client's DB; a flush guard blocks cross-client writes |
| **Accounting software** | **Xero, QuickBooks Online, MYOB Business** behind one connector contract. Each client connects its own account (OAuth), **one software per client**, organisation / company-file picker, one org can't be linked to two clients. **Sync button in the header** + nightly incremental sync: items, contacts, chart of accounts, tax rates |
| **Destinations** | **Accounting software** (draft/approved invoice, quote, sales order - whatever the connected software supports, optional email), **OrderDraft Invoice**, **Custom API** (bearer/api-key/basic auth, field mapping, response id), **Webhook** (HMAC signed). ExportJob log, auto retry with backoff, manual retry/send |
| **Manual entry** | **Manual order** (New order → "Manual entry": customer, products, qty, price override → Save as draft / Create & approve). Manual orders skip the AI and **don't count toward the plan's order limit**. Price can also be overridden on the review screen |
| **Barcode & QR** | Every product gets a barcode: your own EAN-13 / UPC-A / EAN-8 (check digit validated) or Code128, or an **internal EAN-13** made automatically (`20` + product id + check digit - GS1 "in-store" range, never clashes with retail codes). Barcode & QR tab (SVG download), **label sheet printing** (tick products → Print labels: barcode / QR / both, copies, price), `GET /products/lookup?code=` for scanners (barcode, then SKU), search box accepts a scanned code. CSV import column `Barcode / EAN / UPC` |
| **Stock** | `/stock`: tracked products with on hand, reorder level (inline), Low / Out / In stock filters, stock value, **Add / Remove / Count** with reason + note, full movement history (who, when, why, linked order). **OrderDraft is the stock master** for every product with Track inventory on - Xero / QuickBooks / MYOB items too: the first sync brings the opening qty, later syncs never overwrite on hand or the tracking switch. **Approving an order reduces stock** for tracked products (Settings → Stock, on by default; one movement per product, can go negative) |
| **Purchasing** | **Suppliers** (contacts, terms, our account no, CC emails) with **supplier products** (supplier code, agreed cost, min order, lead time, preferred supplier, last cost). **Purchase orders** PO-1001: draft → sent → part received → received / closed / cancelled, ex-GST costs with per-line GST, PDF, emailed from the client's sender, scan barcodes to add lines. **Receive stock** against a PO (partial deliveries, docket no, scan to count, cost per delivery) or **without a PO**; tracked products go into stock with a movement linked to the GRN. **Reorder suggestions** (on hand + on order ≤ reorder level → fill to 2× reorder level, at least min order qty) → one click creates a draft PO per supplier. Stock page shows **On order** |
| **Costing** | **Weighted average cost** per product, recalculated on every receipt (on hand ≤ 0 resets to the receipt cost) - same method as Xero / MYOB inventory. Stock value uses it. Negative stock is allowed |
| **Sorting** | Click column headers (A-Z → Z-A → default) on Products, Customers, Orders, Invoices and Stock. Server-side `?sort=field` / `?sort=-field`, whitelisted columns only |
| **Lifecycle** | Products & customers: **Active / Inactive switch** (inactive = not matched on new orders), **Trash** (soft delete, restore), **Delete forever** from Trash (blocked when used on orders/invoices - kept for records). Tabs: Active · Inactive · All · Trash. Sync / CSV re-import never brings back trashed items or re-activates what you switched off |
| **Customer page** | `/customers/:id`: **Orders & finance** (total sales, orders, average, outstanding, overdue, aging 1-30/31-60/61-90/90+, unpaid invoices, last 6 months sales, recent orders, top products), **Pantry & prices**, details, what the AI learned. Every contact is a customer (supplier flag only comes from accounting sync, not shown) |
| **Pantry (customer prices)** | Per-customer product list with an optional **special sell price** - always, or with a start / expiry date. Used automatically on approve, manual orders, the review screen ("Customer price") and manual invoices; approved orders keep their price even if the pantry changes. "Add usual products" fills it from order history |
| **Customer emails** | **Order confirmation** emailed to the customer when an order is approved (items, qty, total, delivery details; custom subject/message, CC other emails, BCC copy, Resend from the order page). **Sender:** the client's own address once their domain is verified (DKIM + Return-Path via Postmark), otherwise `"Client via OrderDraft" <platform address>` with Reply-To = client. Same sender for invoice emails |
| **Invoices** | Own tax invoices (numbering, GST incl/excl, PDF, email, payments, void, overdue) + **manual invoices** (Invoices → New invoice: custom or product lines, per-line tax rate incl. "No tax", account code, edit while draft, Save & email) + tracking of accounting-software invoices (Xero/QuickBooks webhooks + polling) |
| **Accounts & Tax** | Settings → Accounts & Tax: account codes + tax rates, **import from the connected software** (also refreshed on every sync) or create your own. Defaults per tenant: 200 Sales, 260 Other Revenue, OUTPUT 10%, EXEMPTOUTPUT 0%, BASEXCLUDED 0%. Products pick account/tax from dropdowns; invoice lines use the product's tax rate |
| **SaaS** | 14-day trial, plans (order/user limits), Stripe Checkout + Customer Portal + webhooks, usage meter, soft/hard limits, team invites + roles (owner/admin/reviewer), password reset |
| **Super admin** | Platform stats (MRR, AI cost), tenants list/detail, plan change/trial extend/suspend, **login as owner** (impersonate), plans CRUD |
| **Homepage** | Public marketing page at `/` (hero, channels, how it works, features, integrations, ROI calculator, live pricing from `/public/plans`, FAQ). Logged-in users go straight to the dashboard. Contact email: `CONTACT_EMAIL` in `pages/Landing.tsx` |
| **Reports** | Dashboard: orders, value, AI accuracy, time saved, invoices, channels, top customers, activity log, onboarding checklist |
| **Supplier import** | Suppliers from **CSV/XLSX** (Import data → Suppliers; sample `sample_data/suppliers.csv`) or from **Xero** (contacts with IsSupplier), **QuickBooks** (Vendors), **MYOB** (Supplier cards) - Suppliers page → Import menu, and on every accounting sync. Matched by external id → code → email → name |
| **Supplier terms on POs** | Picking a product on a PO fills the supplier's **cost** and sets qty to the **min order** (amber warning when below; PO detail shows "Below min"); **expected delivery** = order date + longest **lead time** when left empty; reorder suggestions show min order / lead time. Product dialog → **Suppliers** tab |
| **Searchable pickers** | Every product / customer / supplier dropdown is a search box (`components/ui/search-select.tsx`): name, SKU, barcode, code, email, accents ignored, keyboard friendly, fine with 5000+ items |
| **Find anything** | **Ctrl/Cmd + K** command palette + top-bar search: pages, actions (New order, New PO, Receive stock...), live search of orders/products/customers/suppliers, help articles |
| **Help Center** | `/help` knowledge base: 30 articles, every module + "Fields explained" glossary for every form (`frontend/src/help/articles.ts`). Top-bar **Help** opens the article for the current page in a side panel. "Contact support" on every article (`VITE_SUPPORT_EMAIL`, `VITE_SUPPORT_CHAT_URL`, `VITE_SUPPORT_PHONE`) |
| **Audit trail** | Automatic (`core/audit.py`): every create/update/delete of products, customers, suppliers, supplier products, pantry prices, settings, destinations, account codes, tax rates is logged with **old → new values**, the user, and "by support" when a super admin is logged in as the client. Bulk imports/syncs write one summary row. Logins + failed logins logged. **Activity log** page (owner/admin, filters) + **History** tab on product, customer, supplier, PO; super admin sees any client's log on the tenant page |
| **Request log** | Every API request → `logs/app.log` (JSON lines: request id, tenant, user, path, status, ms, IP; rotates daily, `LOG_KEEP_DAYS`). Unhandled errors log the traceback and return *"Something went wrong... reference abc123"* - support runs `grep abc123 logs/app.log*`. `X-Request-ID` header on every response |
| **Categories** | 2-level product categories (Fruit › Citrus), one per product. `/categories` page (add / sub / edit / hide / delete → products become Uncategorised), product form picker, Products filter (parent includes subs) + bulk **Set category**, CSV import column `Category` (`Fruit > Citrus` auto-creates). **Category import** (Import data → Categories, or Categories → Import CSV): one row per category, `Parent` column or `Main > Sub` in the name, optional description / sort order / active; missing parents auto-created, re-import updates (no duplicates), 2 levels max. Sample: `sample_data/categories.csv` |
| **Product page** | `/products/:id` (click a product name): sold qty / value / orders / customers, **every order line with qty** (status filter), stock changes, edit history. History moved out of the edit dialog |
| **Portal logins** | Customer page → **Portal access**: create login → **welcome email** from the client's sender with portal URL, **temporary password** and a **Set your password** link (7 days). First login with the temp password forces a new one. Resend (new temp password), disable, remove, forgot-password. API `/portal/{slug}/login|forgot-password|reset-password`, `/portal/me` (separate `portal` token type). Shop / cart / orders: see Customer portal |
| **Notifications** | Super admin → **Notifications**: title/message, level (info/success/warning/critical), banner or popup, audience all / chosen clients / plans + roles, schedule start/end, closable or not, optional link, **login page** too. Users' X is remembered per user; "show again" resets |
| **Reports** | `/reports`: date presets (incl. AU financial year), summary cards, **Sales by category** (main or with subs), **by customer**, **by order**, CSV download. Sale = approved/exported order, ex GST, by approve date in the client's timezone |
| **Customer portal** | Each client's own online shop at `/portal/<slug>`. **Settings → Customer portal**: on/off, **private** (login first) or **public** (browse without login, optional price hiding), payment options **COD** / **On account**, minimum order, hide out-of-stock, welcome text, checkout note, new-order email + **home page banners** (image, title, link to product / category / URL, schedule, reorder). Customer side: home (banners, "your usual products", categories), shop with search + category / sub-category, product page with photos, cart (server-priced: pantry special price, GST, min order), checkout (delivery date, PO, notes, payment), order-received email, **My orders** (all channels) + **Order again**, profile & delivery address (changes audited as "portal user"). Orders arrive as **Needs review**, source **Portal**, price locked at what the customer saw |
| **Portal app & branding** | Portal installs as a **PWA app** (per-client manifest + icon from `/portal/<slug>/manifest.webmanifest`, service worker `frontend/public/portal-sw.js` with offline page). **Look & feel**: logo, app icon, brand colour, header style, font (Inter / Lora / Nunito), corners, tagline, top-bar message, footer - live preview. **Notify customers**: bell in the portal + **Web Push** to installed apps/browsers + optional email (all / chosen customers, read counts). Staff **Login as** a customer (no password, 2 h, "viewing as" bar, orders marked as staff) and **Set password** (email optional, must-change optional). Central **Customer logins** list |
| **Portal order updates** | Staff **Approve** a portal order → customer gets *"Order #N confirmed"* (bell + phone push + email to the login who ordered; email skipped when the normal order confirmation already goes to that address). **Reject** asks for an optional reason → *"Order #N was not accepted"* + reason. Automatic messages (`portal_notifications.kind = order`) don't show in Notify customers. `portal/notify.py → notify_order_status` |
| **Delivery days & cut-off** | Settings → Customer portal → Setup: delivery weekdays, cut-off time (tenant time zone), days before delivery, closed dates. When set, checkout requires a date from a dropdown of valid dates (earliest pre-selected) and the API rejects wrong days / too-early dates. `GET /portal/<slug>/delivery-dates`, logic in `portal/delivery.py` |
| **Export idempotency** | Each export job has a key `orderdraft-<tenant>-<order>-<job>` that stays the same on every retry: Xero `Idempotency-Key` header, QuickBooks `requestid`, webhooks / custom API `Idempotency-Key` header; MYOB (no key support) looks up invoice `OD<order id>` on retries before creating. Manual **Send** = new job = new document |
| **Category export** | Categories → **Export CSV**: name, parent, description, sort_order, is_active, products - same columns as the import (edit in Excel, import back) |
| **Supplier Trash** | Delete = Trash (soft delete, POs kept), **Trash** tab with Restore / Delete permanently (only without POs / receipts) |
| **Unicode** | All text input cleaned on save (NFC, zero-width/control chars removed, non-breaking space → space); CSV import auto-detects UTF-8 / Windows-1252; PDFs embed DejaVu Sans (₹ € ü emoji-safe; CJK font via `PDF_CJK_FONT`); download filenames RFC 5987; CSV export has BOM for Excel |

## Stack
- **Backend:** FastAPI · SQLAlchemy 2 · Alembic · MySQL (SQLite for local) · background jobs (thread / RQ+Redis) · scheduler
- **Frontend:** React 19 · TypeScript · Vite · Tailwind v4 · shadcn-style components (Radix) · TanStack Query · Recharts · lucide icons
- **Services:** Anthropic Claude, Deepgram, Postmark (inbound + outbound), Xero, QuickBooks Online, MYOB, Stripe

---

## Local setup

### Windows (PowerShell)
```powershell
cd orderdraft-saas\backend
python -m venv venv
venv\Scripts\Activate.ps1          # error aaye: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env             # notepad .env -> ANTHROPIC_API_KEY, DEEPGRAM_API_KEY, SUPERADMIN_EMAILS
pytest                             # 162 tests - AI + Xero/QuickBooks/MYOB mocked, no keys needed
uvicorn app.main:app --reload      # http://localhost:8000/docs
```

### Mac / Linux
```bash
cd orderdraft-saas/backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest
uvicorn app.main:app --reload
```

### Frontend (new terminal)
```bash
cd orderdraft-saas/frontend
npm install
npm run dev                        # http://localhost:5173  (/api is proxied to :8000)
```

On first start the main DB + every client DB are migrated automatically, and the default plans (Starter/Growth/Pro) are created. Each new signup gets its own database (SQLite: `tenant_dbs/od_t_<id>.db`).

> **Upgrading from the earlier single-database build:** delete the old `orderdraft.db` (or MySQL database) once - migrations were reset for the per-client design.

### MySQL instead of SQLite
```sql
CREATE DATABASE orderdraft CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```
**utf8mb4 is required** (emoji / Chinese / Hindi in names). Client databases are created as utf8mb4 automatically; if you created the main DB earlier with `utf8`, run `ALTER DATABASE orderdraft CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;`.
`.env`: `DATABASE_URL=mysql+pymysql://root:@localhost:3306/orderdraft` and restart. Client databases are created on the same server as `od_t_<id>` (override with `TENANT_DATABASE_URL=...{db_name}`); the MySQL user needs **CREATE DATABASE** permission. Tests on MySQL:
`TEST_DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/od_test TEST_TENANT_DATABASE_URL="mysql+pymysql://user:pass@localhost:3306/{db_name}" pytest`

### Demo flow (5 minutes)
1. Register at `/register`. Put your email in `SUPERADMIN_EMAILS` and you also become super admin.
2. **Import data** → Products → `backend/sample_data/products.csv` → match columns → Preview → Import. Do the same for `customers.csv` and `history.csv`.
3. **Destinations** → Add → *OrderDraft Invoice* (or a Webhook pointing at https://webhook.site).
4. **New order** → paste:
   ```
   Hi mate, for tomorrow pls: 2 box romas, 6 cos, 1 tray avos, a box of capsicum - Joe, Joe's Cafe Bondi
   ```
5. In **Orders**, open the draft, fix the capsicum line and Approve. Invoice INV-1001 gets created and emailed (in dev the email prints in the terminal).
6. Send the same message again. The fixed line now matches **alias 98%**. **Customers → Joe → Insights** shows what was learned.

Email webhook test (put your tenant slug in `ToFull` inside `sample_data/postmark_sample.json`):
```bash
curl -u postmark:change-me -H "Content-Type: application/json" -d @sample_data/postmark_sample.json http://localhost:8000/inbound/postmark
# PowerShell: curl.exe ... -d "@sample_data/postmark_sample.json" ...
```

---

## Multi-tenant design (har client ka alag DB)

```
MAIN DB (DATABASE_URL)                       CLIENT DB (od_t_<id>, one per client)
  tenants  (name, slug, plan, Stripe,          products, customers, aliases, history
            db_name, accounting_provider)      inbound messages, draft orders
  users (login), invites, plans                invoices, payments, destinations, export jobs
  usage_events (AI cost, order counters)       connections  (Xero/QBO/MYOB tokens, encrypted)
  integration_links (org id -> tenant)         tenant_settings (business, GST, invoice prefix)
                                               account codes, tax rates, activity log
```
- **Request:** `current_user` loads the user from the main DB and binds the session to *that* client's DB (`bind_tenant`). Product/order models can't be queried until a client is bound, and a session can't switch clients.
- **Jobs / scheduler / webhooks:** always `tenant_session(tenant_id)`; job functions take `tenant_id` as the first argument. Webhooks find the client through `integration_links` (Xero org id / QuickBooks realm id) or the inbound email slug.
- **Safety net:** a `before_flush` guard refuses to save a row whose `tenant_id` differs from the bound client.
- **Dedicated server for a big client:** set `tenants.db_url` (encrypted) - that client's DB can live anywhere.
- **Schema change:** edit the model -> `python -m app.cli makemigrations tenant "add x"` (or `main`) -> `python -m app.cli migrate` (also runs on startup).

**Accounting software rules:** one software per client (switch = disconnect first); one Xero org / QuickBooks company / MYOB file can only belong to one client; tokens live in the client's own DB.

## Folder map

```
backend/
  app/
    main.py                FastAPI app, routers, auto-migrate, scheduler start
    core/                  config, database, security (JWT/bcrypt), crypto (Fernet), deps (roles),
                           jobs (thread/rq/sync), mailer (Postmark), helpers (money, activity log)
    models/                tenant, catalog, orders, integrations, invoices, accounting, logs
    modules/
      auth/                register (trial), login, me, forgot/reset password, accept invite
      catalog/             products, customers, learned aliases, customer insights
      imports/             fields.py (synonyms) · reader.py (CSV/XLSX + suggest) · service.py (upsert) · router (wizard)
      intake/              Postmark parser, PDF, Deepgram, storage, inbound router
      parsing/             extractor (Claude) · matcher (★ core) · pipeline (message → draft)
      orders/              review, edit (price override), manual order, approve (learn + history + exports), CSV
      accounting/          account codes + tax rates: defaults, CRUD, resolve_tax()
      destinations/        base · accounting_dest · internal_invoice · custom_api · webhook · registry · service (jobs/retry)
      invoices/            service (GST per line, numbering, payments, manual create/edit) · pdf (tax invoice) · router
      connectors/          ★ accounting software: base (contract) · api (token refresh, 401/429) · registry
                           xero · quickbooks · myob · service (connect, org picker, one-software rule) · sync · router (+webhooks)
      billing/             limits (usage, trial, hard limit) · stripe_service · router (+webhook)
      team/                invites, roles
      admin/               super admin
      reports/             dashboard, onboarding, activity
      settings/            business + invoice settings
    scheduler.py           per client: retries, stuck messages, invoice status refresh, nightly sync · platform: trial expiry
    migrations.py          upgrade_main / upgrade_tenant / upgrade_all_tenants / make_revision
    worker.py              RQ worker (production)
    cli.py                 make-superadmin / create-superadmin / seed-plans
  alembic/main, alembic/tenant   two migration trees (main DB, client DB)
  tests/                   162 tests (SQLite + MySQL) incl. isolation, Xero, QuickBooks, MYOB
frontend/src/
  lib/                     api.ts (fetch + token), types.ts (API types), utils.ts
  components/ui/           button, input/select/textarea, card, badge, table, dialog, dropdown, switch, tabs, misc
  components/common/       PageHeader, badges, states (empty/loading/pagination/stat), ConfirmDialog
  components/layout/       AppLayout (sidebar, header: usage meter + accounting sync, banners), AuthLayout
  pages/                   Landing (public homepage), Dashboard, orders/, Inbox, invoices/, catalog/, imports/, destinations/,
                           Integrations, settings/ (general, invoice, accounting, team, billing), admin/, auth/
                           orders/ManualOrderForm.tsx, invoices/InvoiceForm.tsx (new + edit draft)
  hooks/                   useMe, useDebounce, useAccounting (accounts / tax rate dropdowns)
```

**Adding a new destination (e.g. MYOB):** create `modules/destinations/myob.py` (`TYPE`, `LABEL`, `SECRET_FIELDS`, `validate_config`, `send` → `ExportResult`) and add one line to `registry.py`. Then add a form for it in the frontend's `Destinations.tsx`.

**Adding a new import field:** add it to `FIELDS` in `modules/imports/fields.py` (with synonyms), then handle it in `service.py`.

## Roles
| | Owner | Admin | Reviewer |
|---|---|---|---|
| Orders review/approve, inbox, new order | ✓ | ✓ | ✓ |
| Catalog edit, imports, destinations, integrations, invoices actions, settings | ✓ | ✓ | view only |
| Team | ✓ | ✓ (cannot touch owners) | - |
| Billing | ✓ | - | - |

---

## Production

### Processes
```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2   # API
python -m app.worker                                             # RQ worker   (JOBS_MODE=rq)
python -m app.scheduler                                          # scheduler   (RUN_SCHEDULER_IN_APP=false)
```
`.env`: `JOBS_MODE=rq`, `REDIS_URL=...`, `RUN_SCHEDULER_IN_APP=false`, a strong `SECRET_KEY` + `ENCRYPTION_KEY`, the MySQL URL, `FRONTEND_URL=https://app.yourdomain.com`.
Frontend: `VITE_API_URL=https://api.yourdomain.com npm run build`, then serve `dist/` from Vercel/Netlify/Nginx (SPA fallback to `index.html`).
Web Push: run `python -m app.cli vapid-keys` once and put `VAPID_PRIVATE_KEY` + `VAPID_SUBJECT` in `.env` (without them the bell + email still work, phone push is off). The portal must be served over **https** for install + push. Files (`STORAGE_DIR`) and logs (`LOG_DIR`, default `./logs`) go on a persistent disk. Later you can move them to S3/R2 by changing only `intake/storage.py`.

### Third-party setup
1. **Postmark inbound:** Inbound stream → point your domain's MX record (`orders.yourdomain.com`) to Postmark → webhook `https://postmark:<pass>@api.yourdomain.com/inbound/postmark`. Tenant address: `<slug>@orders.yourdomain.com`.
2. **Postmark outbound:** use the server token as `POSTMARK_SERVER_TOKEN` and set `MAIL_FROM` to a verified sender (invites, password reset, fallback sender for client emails).
   - **Client sender domains** (Settings → Email): set `POSTMARK_ACCOUNT_TOKEN` (Postmark → Account → API Tokens - the *account* token, not the server token). When a client saves `orders@theirdomain.com.au` we create the domain in Postmark and show 2 DNS records: **TXT** `<selector>._domainkey` (DKIM) and **CNAME** `pm-bounces` → `pm.mtasv.net` (Return-Path). They don't need to edit SPF - the Return-Path CNAME gives SPF alignment. Verify button + automatic re-check every 3 hours. Free mail (gmail, outlook, bigpond...) is refused, and a domain can belong to only one client (main-DB `integration_links`, provider `email_domain`).
3. **Accounting apps** (one platform app per software; every client connects its own account through it):
   - **Xero:** developer.xero.com → Web app → redirect `https://api.yourdomain.com/integrations/xero/callback` → client id/secret. Webhooks → `https://api.yourdomain.com/webhooks/xero` (INVOICE events) → put the key in `XERO_WEBHOOK_KEY`. Default scopes are the granular ones (`offline_access accounting.invoices accounting.contacts accounting.settings`) - Web apps created since March 2026 only get these. Old apps on broad scopes: override `XERO_SCOPES`. Commercial launch par connection limits / app certification / API pricing check karo.
   - **QuickBooks Online:** developer.intuit.com → app → redirect `https://api.yourdomain.com/integrations/quickbooks/callback`, scope `com.intuit.quickbooks.accounting`. `QUICKBOOKS_ENVIRONMENT=sandbox` for testing, `production` after Intuit app review. Webhooks → `https://api.yourdomain.com/webhooks/quickbooks` (Invoice) → verifier token in `QUICKBOOKS_WEBHOOK_VERIFIER`.
   - **MYOB Business:** developer.myob.com → app → redirect `https://api.yourdomain.com/integrations/myob/callback`; API key = `MYOB_CLIENT_ID`. Match `MYOB_SCOPES` to your app. No webhooks - status comes from polling.
4. **Stripe:** create a Product + monthly AUD Price for each plan → **Super admin → Plans** pe `price_...` id daalo. Webhook `https://api.yourdomain.com/webhooks/stripe` (checkout.session.completed, customer.subscription.created/updated/deleted). Enable the Customer Portal.
5. **Deepgram / Anthropic:** API keys. Per-tenant AI cost estimates show up in Super admin (adjust the rates in `.env`).

### Data / GST notes
- Fresh food is often **GST-free** in AU. Set the import `GST` column to "GST free", or switch the toggle on the product. Products synced from accounting software get this from their tax type.
- Money is stored as `Numeric(12,2)` and rounded with `Decimal` (ROUND_HALF_UP).
- **Tax per invoice line:** line tax code (if it exists in Tax rates) → its rate; otherwise the product's GST switch → tenant GST rate or 0. A manual line can pick "No tax". Each line stores `account_code`, `tax_code`, `tax_rate`.
- Tax/account codes: Xero uses its own codes (`OUTPUT`, `200`); QuickBooks stores the TaxCode/Account **Id** as the code; MYOB uses `GST`/`FRE` and `4-1100` with the UID kept as `external_id`. Imported rows keep their code (edit it in the software); the next sync overwrites name and rate. If a client switches software, the old software's codes are made inactive.
- Product/customer `external_id` is only trusted when its `source` is the connected software (a Xero ItemID means nothing to QuickBooks).

## Known limits / next steps
- Super admin stats (orders / messages) come from main-DB usage events, so no client DB is opened for the dashboard; the tenant detail page opens that one client's DB.
- SMS/voicemail number (Twilio AU), Unleashed/Cin7 destinations, auto-approve above a threshold, and embeddings for 5k+ SKU catalogs are planned.
- **Roadmap - customer portal:** every client gets its own ordering portal + mobile PWA where their customers log in, browse products (with the photos above), reorder "usual" items and place orders themselves, plus push/email notifications (order received, approved, invoiced). Groundwork done: product photos with public media URLs, full customer contact/address data, order confirmation emails and sender domains.
- **Roadmap - stock & purchasing:** Suppliers, Purchase orders and Stock received are built. Next: supplier bills to accounting (Xero / QBO / MYOB) → returns & stocktake → multi-location / POS. Suppliers import from CSV and from Xero / QuickBooks / MYOB (also on every sync). Plan doc: OrderDraft - Stock & Purchasing Roadmap (Claude Docs).
- Product photos are stored on local disk (`STORAGE_DIR/t<id>/products`). For several servers, move them to S3/R2 (same file names).
- MySQL DATETIME only stores seconds, so lists use `created_at, id` ordering.