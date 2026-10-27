"""
App config - sab settings .env file se aati hain.
Naya setting add karna ho to yahan add karo aur .env.example mein bhi likho.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # --- App ---
    APP_NAME = os.getenv("APP_NAME", "OrderDraft")
    APP_URL = os.getenv("APP_URL", "http://localhost:8000")            # backend public URL
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-dev-secret-change-me")
    # Xero tokens / API secrets encrypt karne ke liye. Khali = SECRET_KEY se derive.
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
    TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "72"))
    # Ye emails register/login par super admin ban jate hain (comma separated)
    SUPERADMIN_EMAILS = [e.strip().lower() for e in os.getenv("SUPERADMIN_EMAILS", "").split(",") if e.strip()]

    # --- Database ---
    # MySQL: mysql+pymysql://user:pass@localhost:3306/orderdraft
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orderdraft.db")
    # Har client ka apna database. {db_name} replace hota hai (od_t_<tenant id>).
    # Khali = DATABASE_URL wala hi server (MySQL) / ./tenant_dbs/ folder (SQLite).
    # MySQL user ko CREATE DATABASE permission chahiye.
    TENANT_DATABASE_URL = os.getenv("TENANT_DATABASE_URL", "")
    TENANT_DB_PREFIX = os.getenv("TENANT_DB_PREFIX", "od_t_")
    # Startup par "alembic upgrade head" apne aap chalao
    AUTO_MIGRATE = _bool("AUTO_MIGRATE", "true")

    # --- Background jobs ---
    # thread = app ke andar thread (local/Windows ke liye)
    # rq     = Redis queue (production) - REDIS_URL chahiye + "python -m app.worker"
    # sync   = turant usi request mein (tests)
    JOBS_MODE = os.getenv("JOBS_MODE", "thread")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # Scheduler (retries, nightly Xero sync, trial expiry) app ke andar hi chalao?
    # Local = true. Production (multiple workers) = false + alag process: python -m app.scheduler
    RUN_SCHEDULER_IN_APP = _bool("RUN_SCHEDULER_IN_APP", "true")

    # --- File storage ---
    STORAGE_DIR = os.getenv("STORAGE_DIR", "./storage")
    # Request / error log (JSON lines, roz rotate, 30 din rakhte hain) - support yahin se dekhta hai
    LOG_DIR = os.getenv("LOG_DIR", "./logs")
    # Web Push (customer portal app notifications) - keys: python -m app.cli vapid-keys
    VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:support@orderdraft.com.au")
    LOG_KEEP_DAYS = int(os.getenv("LOG_KEEP_DAYS", "30"))

    # --- Claude ---
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
    # Cost estimate (USD per million tokens) - super admin dashboard ke liye
    CLAUDE_INPUT_COST_PER_M = float(os.getenv("CLAUDE_INPUT_COST_PER_M", "3"))
    CLAUDE_OUTPUT_COST_PER_M = float(os.getenv("CLAUDE_OUTPUT_COST_PER_M", "15"))

    # --- Deepgram ---
    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
    DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "en-AU")
    DEEPGRAM_COST_PER_MIN = float(os.getenv("DEEPGRAM_COST_PER_MIN", "0.0043"))

    # --- Inbound email (Postmark) ---
    INBOUND_DOMAIN = os.getenv("INBOUND_DOMAIN", "orders.example.com")
    INBOUND_WEBHOOK_USER = os.getenv("INBOUND_WEBHOOK_USER", "postmark")
    INBOUND_WEBHOOK_PASS = os.getenv("INBOUND_WEBHOOK_PASS", "change-me")

    # --- Outbound email (Postmark) - invites, password reset, invoices ---
    # Khali = emails console/log mein print honge (dev)
    POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")
    MAIL_FROM = os.getenv("MAIL_FROM", "OrderDraft <no-reply@example.com>")
    MAIL_PLATFORM_NAME = os.getenv("MAIL_PLATFORM_NAME", "OrderDraft")   # "Fresh Farms via OrderDraft"
    # Postmark ACCOUNT token (server token nahi) - client ka sender domain add/verify karne ke liye.
    # Khali = dev mode: DNS records dikhte hain par asli verify nahi hota.
    POSTMARK_ACCOUNT_TOKEN = os.getenv("POSTMARK_ACCOUNT_TOKEN", "")

    # --- Accounting software (platform ka ek app per software; har client apna account connect karta hai) ---
    # Redirect URI har developer portal mein register karo: {APP_URL}/integrations/<provider>/callback
    # --- Xero ---
    XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID", "")
    XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET", "")
    XERO_REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", f"{APP_URL}/integrations/xero/callback")
    # Granular scopes (March 2026 se naye Web apps ke liye; broad accounting.transactions Sep 2027 tak hi)
    #   accounting.invoices  -> Invoices, Quotes (invoice email, status)
    #   accounting.contacts  -> Contacts (customers)
    #   accounting.settings  -> Items, Accounts, TaxRates
    #   offline_access       -> refresh token (iske bina connection 30 min mein toot jaata hai)
    # Purana app sirf broad scopes allow kare to .env mein XERO_SCOPES override karo.
    XERO_SCOPES = os.getenv(
        "XERO_SCOPES",
        "offline_access accounting.invoices accounting.contacts accounting.settings",
    )
    XERO_WEBHOOK_KEY = os.getenv("XERO_WEBHOOK_KEY", "")

    # --- QuickBooks Online (Intuit developer portal) ---
    QUICKBOOKS_CLIENT_ID = os.getenv("QUICKBOOKS_CLIENT_ID", "")
    QUICKBOOKS_CLIENT_SECRET = os.getenv("QUICKBOOKS_CLIENT_SECRET", "")
    QUICKBOOKS_REDIRECT_URI = os.getenv("QUICKBOOKS_REDIRECT_URI", f"{APP_URL}/integrations/quickbooks/callback")
    QUICKBOOKS_ENVIRONMENT = os.getenv("QUICKBOOKS_ENVIRONMENT", "sandbox")   # sandbox | production
    QUICKBOOKS_SCOPES = os.getenv("QUICKBOOKS_SCOPES", "com.intuit.quickbooks.accounting")
    QUICKBOOKS_WEBHOOK_VERIFIER = os.getenv("QUICKBOOKS_WEBHOOK_VERIFIER", "")
    QUICKBOOKS_MINOR_VERSION = os.getenv("QUICKBOOKS_MINOR_VERSION", "75")

    # --- MYOB Business / AccountRight (my.myob.com developer) ---
    MYOB_CLIENT_ID = os.getenv("MYOB_CLIENT_ID", "")          # = API key (x-myobapi-key)
    MYOB_CLIENT_SECRET = os.getenv("MYOB_CLIENT_SECRET", "")
    MYOB_REDIRECT_URI = os.getenv("MYOB_REDIRECT_URI", f"{APP_URL}/integrations/myob/callback")
    # Apne MYOB app ke allowed scopes se match karo (purane apps: "CompanyFile")
    MYOB_SCOPES = os.getenv("MYOB_SCOPES", "CompanyFile")

    # --- Stripe ---
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
    # Limit ka kitna % tak orders process karte rahein (client ke orders kabhi achanak na rukein)
    HARD_LIMIT_PERCENT = int(os.getenv("HARD_LIMIT_PERCENT", "120"))

    # --- Matching ---
    MATCH_AUTO_THRESHOLD = float(os.getenv("MATCH_AUTO_THRESHOLD", "85"))
    MATCH_MIN_THRESHOLD = float(os.getenv("MATCH_MIN_THRESHOLD", "55"))

    # --- Reports: "time saved" estimate ---
    MANUAL_SECONDS_PER_ORDER = int(os.getenv("MANUAL_SECONDS_PER_ORDER", "120"))
    MANUAL_SECONDS_PER_LINE = int(os.getenv("MANUAL_SECONDS_PER_LINE", "30"))


settings = Settings()
