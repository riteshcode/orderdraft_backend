"""
Product images (aage customer web portal / PWA mein dikhenge).

  GET    /products/{id}/images
  POST   /products/{id}/images               multipart: files (ek ya zyada)
  PATCH  /products/{id}/images/{image_id}    {alt_text}
  POST   /products/{id}/images/{image_id}/primary
  PUT    /products/{id}/images/order         {ids: [3, 1, 2]}
  DELETE /products/{id}/images/{image_id}

  GET    /media/t{tenant_id}/products/{file} public file (naam uuid hai - guess nahi ho sakta)

Files: STORAGE_DIR/t<tenant_id>/products/<uuid>.jpg  (+ <uuid>_thumb.jpg)
"""
import io
import os
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_role
from app.models import Product, ProductImage, User

router = APIRouter(tags=["catalog"])
admin_only = require_role("owner", "admin")

MAX_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 8
MAX_SIDE = 1600
THUMB_SIDE = 400
FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}
FILE_RE = re.compile(r"^[0-9a-f]{32}(_thumb)?\.(jpg|png|webp)$")


class AltIn(BaseModel):
    alt_text: str | None = None


class OrderIn(BaseModel):
    ids: list[int]


# ---------------- helpers ----------------
def image_dir(tenant_id: int) -> str:
    return os.path.join(settings.STORAGE_DIR, f"t{int(tenant_id)}", "products")


def media_url(tenant_id: int, file_name: str | None) -> str | None:
    if not file_name:
        return None
    return f"{settings.APP_URL.rstrip('/')}/media/t{tenant_id}/products/{file_name}"


def image_out(img: ProductImage) -> dict:
    return {"id": img.id, "url": media_url(img.tenant_id, img.file_name),
            "thumb_url": media_url(img.tenant_id, img.thumb_name or img.file_name),
            "is_primary": img.is_primary, "sort_order": img.sort_order, "alt_text": img.alt_text,
            "width": img.width, "height": img.height, "size_bytes": img.size_bytes}


def sorted_images(product: Product) -> list[ProductImage]:
    return sorted(product.images, key=lambda i: (not i.is_primary, i.sort_order, i.id))


def primary_thumb(product: Product) -> str | None:
    imgs = sorted_images(product)
    return image_out(imgs[0])["thumb_url"] if imgs else None


def _get_product(db: Session, user: User, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if not p or p.tenant_id != user.tenant_id:
        raise HTTPException(404, "Product not found")
    return p


def _get_image(db: Session, product: Product, image_id: int) -> ProductImage:
    img = db.get(ProductImage, image_id)
    if not img or img.product_id != product.id or img.tenant_id != product.tenant_id:
        raise HTTPException(404, "Image not found")
    return img


def _save(im: Image.Image, path: str, fmt: str):
    if fmt == "JPEG":
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(path, "JPEG", quality=85, optimize=True, progressive=True)
    elif fmt == "WEBP":
        im.save(path, "WEBP", quality=85)
    else:
        im.save(path, "PNG", optimize=True)


def process_upload(tenant_id: int, data: bytes, original_name: str | None) -> dict:
    """Validate (Pillow se - sirf content-type pe bharosa nahi), EXIF rotate, resize, thumbnail."""
    if len(data) > MAX_BYTES:
        raise HTTPException(400, f"{original_name}: images larger than 5 MB aren't allowed")
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        im.verify()                          # corrupt file pakdo
        im = Image.open(io.BytesIO(data))    # verify ke baad dobara kholna padta hai
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, f"{original_name}: this isn't an image file")
    if fmt not in FORMATS:
        raise HTTPException(400, f"{original_name}: only JPG, PNG or WEBP images are allowed")

    ext, mime = FORMATS[fmt]
    im = ImageOps.exif_transpose(im)
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    thumb = im.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))

    folder = image_dir(tenant_id)
    os.makedirs(folder, exist_ok=True)
    key = uuid.uuid4().hex
    file_name, thumb_name = f"{key}.{ext}", f"{key}_thumb.{ext}"
    _save(im, os.path.join(folder, file_name), fmt)
    _save(thumb, os.path.join(folder, thumb_name), fmt)
    return {"file_name": file_name, "thumb_name": thumb_name, "content_type": mime, "width": im.width,
            "height": im.height, "size_bytes": os.path.getsize(os.path.join(folder, file_name)),
            "original_name": (original_name or "")[:255] or None}


def delete_files(img: ProductImage):
    for name in (img.file_name, img.thumb_name):
        if name and FILE_RE.match(name):
            try:
                os.remove(os.path.join(image_dir(img.tenant_id), name))
            except FileNotFoundError:
                pass


def _ensure_primary(product: Product):
    imgs = sorted_images(product)
    if imgs and not any(i.is_primary for i in imgs):
        imgs[0].is_primary = True


# ---------------- endpoints ----------------
@router.get("/products/{product_id}/images")
def list_images(product_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return [image_out(i) for i in sorted_images(_get_product(db, user, product_id))]


@router.post("/products/{product_id}/images")
async def upload_images(product_id: int, files: list[UploadFile] = File(...),
                        user: User = Depends(admin_only), db: Session = Depends(get_db)):
    product = _get_product(db, user, product_id)
    if len(product.images) + len(files) > MAX_IMAGES:
        raise HTTPException(400, f"A product can have up to {MAX_IMAGES} images")
    saved: list[dict] = []
    try:
        for f in files:
            data = await f.read(MAX_BYTES + 1)
            saved.append(process_upload(user.tenant_id, data, f.filename))
    except HTTPException:
        for s in saved:   # aadhi upload hui files hata do
            delete_files(ProductImage(tenant_id=user.tenant_id, file_name=s["file_name"], thumb_name=s["thumb_name"]))
        raise

    next_order = max((i.sort_order for i in product.images), default=-1) + 1
    for n, s in enumerate(saved):
        product.images.append(ProductImage(tenant_id=user.tenant_id, sort_order=next_order + n, is_primary=False, **s))
    _ensure_primary(product)
    db.commit()
    db.refresh(product)
    return [image_out(i) for i in sorted_images(product)]


@router.patch("/products/{product_id}/images/{image_id}")
def update_image(product_id: int, image_id: int, data: AltIn, user: User = Depends(admin_only),
                 db: Session = Depends(get_db)):
    img = _get_image(db, _get_product(db, user, product_id), image_id)
    img.alt_text = (data.alt_text or "").strip()[:255] or None
    db.commit()
    return image_out(img)


@router.post("/products/{product_id}/images/{image_id}/primary")
def set_primary(product_id: int, image_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    product = _get_product(db, user, product_id)
    target = _get_image(db, product, image_id)
    for i in product.images:
        i.is_primary = i.id == target.id
    db.commit()
    return [image_out(i) for i in sorted_images(product)]


@router.put("/products/{product_id}/images/order")
def reorder(product_id: int, data: OrderIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    product = _get_product(db, user, product_id)
    by_id = {i.id: i for i in product.images}
    if sorted(data.ids) != sorted(by_id):
        raise HTTPException(400, "Include all image IDs for this product")
    for n, image_id in enumerate(data.ids):
        by_id[image_id].sort_order = n
    db.commit()
    return [image_out(i) for i in sorted_images(product)]


@router.delete("/products/{product_id}/images/{image_id}")
def delete_image(product_id: int, image_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    product = _get_product(db, user, product_id)
    img = _get_image(db, product, image_id)
    delete_files(img)
    product.images.remove(img)
    db.delete(img)
    db.flush()
    _ensure_primary(product)
    db.commit()
    return [image_out(i) for i in sorted_images(product)]


@router.get("/media/t{tenant_id}/products/{file_name}", include_in_schema=False)
def media(tenant_id: int, file_name: str):
    """Public (customer portal / email ke liye). Naam strict regex se - path traversal nahi."""
    if not FILE_RE.match(file_name):
        raise HTTPException(404, "Not found")
    path = os.path.join(image_dir(tenant_id), file_name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    ext = file_name.rsplit(".", 1)[1]
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "public, max-age=31536000, immutable"})
