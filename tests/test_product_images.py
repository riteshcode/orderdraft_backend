"""Product images: upload, resize, thumbnail, primary/reorder/delete, public media, client isolation."""
import io

from PIL import Image

from tests.conftest import register


def png(w=2400, h=1200, color=(200, 30, 30), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, fmt)
    return buf.getvalue()


def make_product(client, h, sku="TOM-1"):
    return client.post("/products", headers=h, json={"sku": sku, "name": "Roma tomatoes", "price": 32.5}).json()["id"]


def upload(client, h, pid, *files):
    return client.post(f"/products/{pid}/images", headers=h,
                       files=[("files", (name, data, "image/png")) for name, data in files])


def media_path(url: str) -> str:
    return url[url.index("/media/"):]


def test_upload_resizes_and_makes_thumb(client, auth):
    pid = make_product(client, auth)
    r = upload(client, auth, pid, ("big.png", png()), ("b.jpg", png(300, 300, fmt="JPEG")))
    assert r.status_code == 200, r.text
    imgs = r.json()
    assert len(imgs) == 2 and imgs[0]["is_primary"] and not imgs[1]["is_primary"]
    assert (imgs[0]["width"], imgs[0]["height"]) == (1600, 800)      # max 1600px
    thumb = client.get(media_path(imgs[0]["thumb_url"]))
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert max(Image.open(io.BytesIO(thumb.content)).size) == 400
    assert "immutable" in thumb.headers["cache-control"]

    p = client.get("/products", headers=auth).json()["items"][0]
    assert p["image_url"] == imgs[0]["thumb_url"] and len(p["images"]) == 2


def test_rejects_non_images_and_limits(client, auth):
    pid = make_product(client, auth)
    r = upload(client, auth, pid, ("evil.png", b"<?php echo 1; ?>"))
    assert r.status_code == 400 and "image" in r.json()["detail"]
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    assert upload(client, auth, pid, ("a.gif", gif.getvalue())).status_code == 400
    assert upload(client, auth, pid, ("huge.png", b"0" * (5 * 1024 * 1024 + 10))).status_code == 400
    ok = upload(client, auth, pid, *[(f"{i}.png", png(50, 50)) for i in range(8)])
    assert ok.status_code == 200
    assert upload(client, auth, pid, ("9.png", png(50, 50))).status_code == 400   # max 8


def test_primary_reorder_alt_delete(client, auth):
    pid = make_product(client, auth)
    imgs = upload(client, auth, pid, ("1.png", png(50, 50)), ("2.png", png(60, 60)), ("3.png", png(70, 70))).json()
    a, b, c = (i["id"] for i in imgs)

    imgs = client.post(f"/products/{pid}/images/{c}/primary", headers=auth).json()
    assert imgs[0]["id"] == c and imgs[0]["is_primary"]

    imgs = client.put(f"/products/{pid}/images/order", headers=auth, json={"ids": [b, a, c]}).json()
    assert [i["id"] for i in imgs] == [c, b, a]             # primary pehle, phir order
    assert client.put(f"/products/{pid}/images/order", headers=auth, json={"ids": [a]}).status_code == 400

    assert client.patch(f"/products/{pid}/images/{b}", headers=auth,
                        json={"alt_text": "Red roma tomatoes"}).json()["alt_text"] == "Red roma tomatoes"

    old_url = media_path(imgs[0]["url"])
    imgs = client.delete(f"/products/{pid}/images/{c}", headers=auth).json()
    assert [i["id"] for i in imgs] == [b, a] and imgs[0]["is_primary"]   # agla primary ban gaya
    assert client.get(old_url).status_code == 404                        # file bhi gayi


def test_images_isolated_between_clients(client, auth):
    other = register(client, "b@ocean.com.au", "Ocean Seafood Co")
    pid = make_product(client, auth)
    img = upload(client, auth, pid, ("1.png", png(50, 50))).json()[0]
    assert client.get(f"/products/{pid}/images", headers=other).status_code == 404
    assert client.delete(f"/products/{pid}/images/{img['id']}", headers=other).status_code == 404
    other_pid = make_product(client, other, "FISH-1")
    # Client B apne product ke URL se A ki image nahi chhu sakta
    assert client.delete(f"/products/{other_pid}/images/{img['id']}", headers=other).status_code == 404
    assert client.get(f"/products/{pid}/images", headers=auth).json()[0]["id"] == img["id"]


def test_media_path_traversal_blocked(client, auth):
    for bad in ("..%2F..%2Fapp.db", "x.php", "abc.jpg", "%2e%2e"):
        assert client.get(f"/media/t1/products/{bad}").status_code == 404
