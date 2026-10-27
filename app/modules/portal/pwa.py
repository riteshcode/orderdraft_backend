"""
Customer portal as an app (PWA) - phone home screen pe "Install" / "Add to Home Screen".

  GET /portal/{slug}/manifest.webmanifest     app ka naam, colour, icons, start page
  GET /portal/{slug}/icon-{size}.png          192 / 512 / 180 (iPhone) - admin ka app icon, warna logo,
                                              warna business ke initials brand colour pe
Service worker (offline page + push notifications): frontend/public/portal-sw.js
"""
import io
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.helpers import get_tenant_settings
from app.modules.catalog.images import image_dir
from app.modules.portal.auth import _tenant

router = APIRouter(prefix="/portal", tags=["portal-pwa"])

SIZES = (180, 192, 512)
DEFAULT_COLOR = "#2563eb"
FONT = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts", "DejaVuSans-Bold.ttf")


@router.get("/{slug}/manifest.webmanifest")
def manifest(slug: str, db: Session = Depends(get_db)):
    t = _tenant(db, slug)
    cfg = get_tenant_settings(db, t.id)
    name = cfg.business_name or t.name
    base = f"{settings.FRONTEND_URL}/portal/{t.slug}"
    icon = f"{settings.APP_URL.rstrip('/')}/portal/{t.slug}/icon"
    color = cfg.portal_brand_color or DEFAULT_COLOR
    # Status bar = header ka rang; app khulte waqt (Android splash) rangeen header ho to brand rang, warna safed
    header = {"brand": color, "dark": "#0f172a"}.get(cfg.portal_header_style or "light", "#ffffff")
    data = {
        "id": base, "name": name, "short_name": name[:12], "description": cfg.portal_tagline or f"Order from {name}",
        "start_url": base, "scope": f"{base}/", "display": "standalone", "display_override": ["standalone"],
        "orientation": "portrait", "background_color": header, "theme_color": header,
        "categories": ["shopping", "food"], "prefer_related_applications": False,
        # App icon pe long-press (Android) -> seedha yahan
        "shortcuts": [{"name": "Shop", "url": f"{base}/shop", "icons": [{"src": f"{icon}-192.png", "sizes": "192x192"}]},
                      {"name": "Cart", "url": f"{base}/cart", "icons": [{"src": f"{icon}-192.png", "sizes": "192x192"}]},
                      {"name": "My orders", "url": f"{base}/orders",
                       "icons": [{"src": f"{icon}-192.png", "sizes": "192x192"}]}],
        "icons": [{"src": f"{icon}-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": f"{icon}-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": f"{icon}-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }
    return JSONResponse(data, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=300"})


def _initials(name: str) -> str:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalnum() and w.lower() not in ("pty", "ltd", "the")]
    return ("".join(w[0] for w in words[:2]) or name[:1] or "?").upper()


def make_icon(name: str, color: str | None, source_path: str | None, size: int) -> bytes:
    bg = color or DEFAULT_COLOR
    if source_path and os.path.exists(source_path):
        # Admin ka image: square mein, thoda padding (maskable safe zone), white background
        src = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")
        canvas = Image.new("RGBA", (size, size), "white")
        inner = int(size * 0.8)
        src.thumbnail((inner, inner))
        canvas.paste(src, ((size - src.width) // 2, (size - src.height) // 2), src)
        im = canvas.convert("RGB")
    else:
        im = Image.new("RGB", (size, size), bg)
        draw = ImageDraw.Draw(im)
        text = _initials(name)
        try:
            font = ImageFont.truetype(FONT, int(size * (0.42 if len(text) > 1 else 0.5)))
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
                  text, fill="white", font=font)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


@router.get("/{slug}/icon-{size}.png")
def icon(slug: str, size: int, db: Session = Depends(get_db)):
    if size not in SIZES:
        raise HTTPException(404, "Not found")
    t = _tenant(db, slug)
    cfg = get_tenant_settings(db, t.id)
    file_name = cfg.portal_icon or cfg.portal_logo
    path = os.path.join(image_dir(t.id), file_name) if file_name else None
    png = make_icon(cfg.business_name or t.name, cfg.portal_brand_color, path, size)
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
