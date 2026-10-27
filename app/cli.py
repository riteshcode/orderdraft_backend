"""
Chhote admin commands:
    python -m app.cli make-superadmin you@example.com
    python -m app.cli create-superadmin you@example.com "Your Name" password123
    python -m app.cli seed-plans
    python -m app.cli migrate                       # main DB + har client ka DB upgrade
    python -m app.cli makemigrations tenant "add x" # model badla -> migration file (main | tenant)
    python -m app.cli tenants                       # clients aur unke databases
    python -m app.cli vapid-keys                    # Web Push keys (customer portal app notifications)
"""
import sys

from sqlalchemy import select

from app.core.database import SessionLocal, provision_tenant, tenant_db_name
from app.core.security import hash_password
from app.models import Tenant, User
from app.seed import seed_plans


def main(args: list[str]):
    if not args:
        print(__doc__)
        return
    db = SessionLocal()
    try:
        cmd = args[0]
        if cmd == "vapid-keys":
            from app.core.webpush import generate_vapid_keys
            private, public = generate_vapid_keys()
            print("Add to backend .env (keep the private key secret):\n")
            print(f"VAPID_PRIVATE_KEY={private}")
            print("VAPID_SUBJECT=mailto:support@yourdomain.com")
            print(f"\n(public key, sent to browsers automatically: {public})")
            return
        if cmd == "make-superadmin" and len(args) == 2:
            user = db.scalars(select(User).where(User.email == args[1].lower())).first()
            if not user:
                print("User not found - pehle register karo")
                return
            user.is_superadmin = True
            db.commit()
            print(f"{user.email} is now a super admin")

        elif cmd == "create-superadmin" and len(args) == 4:
            email, name, password = args[1].lower(), args[2], args[3]
            if db.scalars(select(User).where(User.email == email)).first():
                print("User already exists - make-superadmin use karo")
                return
            tenant = Tenant(name="Platform Admin", slug="platformadmin", subscription_status="active")
            db.add(tenant)
            db.flush()
            tenant.db_name = tenant_db_name(tenant.id)
            db.add(User(tenant_id=tenant.id, name=name, email=email, password_hash=hash_password(password),
                        role="owner", is_superadmin=True))
            db.commit()
            provision_tenant(tenant)
            tenant.db_ready = True
            db.commit()
            print(f"Super admin {email} created")

        elif cmd == "migrate":
            from app.migrations import upgrade_all_tenants, upgrade_main
            upgrade_main()
            print("Main DB upgraded", upgrade_all_tenants())

        elif cmd == "makemigrations" and len(args) == 3 and args[1] in ("main", "tenant"):
            from app.migrations import make_revision
            make_revision(args[1], args[2])

        elif cmd == "tenants":
            for t in db.scalars(select(Tenant).order_by(Tenant.id)):
                print(f"{t.id:>4}  {t.slug:<25} db={t.db_name} ready={t.db_ready} "
                      f"software={t.accounting_provider or '-'} status={t.status}/{t.subscription_status}")

        elif cmd == "seed-plans":
            seed_plans(db)
            print("Plans seeded")
        else:
            print(__doc__)
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
