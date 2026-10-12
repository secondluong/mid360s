"""Asset serving must stay cheap on the MyView relay (4G uplink).

The icon models and the three.js bundle are megabytes uncompressed; over the
relay they time out and the viewer falls back to the flat card.
"""

import gzip
import shutil

import pytest

import server


class FakeRequest:
    def __init__(self, accept_encoding=""):
        self.headers = {"Accept-Encoding": accept_encoding}
        self.match_info = {}


@pytest.fixture
def base(tmp_path):
    (tmp_path / "jsm" / "loaders").mkdir(parents=True)
    return tmp_path


def write(path, body=b"x" * 4096):
    path.write_bytes(body)
    return path


def gz_sibling(src):
    out = src.with_suffix(src.suffix + ".gz")
    with src.open("rb") as fh, gzip.open(out, "wb") as gz:
        shutil.copyfileobj(fh, gz)
    shutil.copystat(src, out)
    return out


def test_plain_file_when_client_cannot_gunzip(base):
    src = write(base / "model.FBX")
    gz_sibling(src)
    resp = server.asset_response(FakeRequest(""), base, "model.FBX")
    assert "Content-Encoding" not in resp.headers
    assert resp.headers["Cache-Control"].startswith("public, max-age=")


def test_precompressed_when_accepted(base):
    src = write(base / "model.FBX")
    gz = gz_sibling(src)
    resp = server.asset_response(FakeRequest("gzip, deflate"), base, "model.FBX")
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.headers["Content-Type"] == "application/octet-stream"
    assert resp._path == gz


def test_stale_gz_is_ignored(base):
    src = write(base / "three.min.js")
    gz = gz_sibling(src)
    import os

    os.utime(gz, (0, 0))
    resp = server.asset_response(FakeRequest("gzip"), base, "three.min.js")
    assert "Content-Encoding" not in resp.headers
    assert resp._path == src


def test_content_type_follows_the_original_name(base):
    src = write(base / "three.min.js")
    gz_sibling(src)
    resp = server.asset_response(FakeRequest("gzip"), base, "three.min.js")
    assert "javascript" in resp.headers["Content-Type"]


def test_nested_path_is_served(base):
    write(base / "jsm" / "loaders" / "FBXLoader.js")
    resp = server.asset_response(FakeRequest(""), base, "jsm/loaders/FBXLoader.js")
    assert resp._path.name == "FBXLoader.js"


@pytest.mark.parametrize(
    "name",
    ["../server.py", "jsm/../../server.py", "/etc/passwd", "", "jsm//x.js"],
)
def test_path_traversal_is_rejected(base, name):
    from aiohttp import web

    with pytest.raises(web.HTTPNotFound):
        server.asset_response(FakeRequest(""), base, name)


def test_missing_file_is_not_found(base):
    from aiohttp import web

    with pytest.raises(web.HTTPNotFound):
        server.asset_response(FakeRequest(""), base, "nope.FBX")


# The MyView host app only forwards subresource requests whose URL ends in one of
# a few extensions; .FBX and .webp are dropped before leaving its browser, so the
# page asks for "<real name>.bin". Without these the icon silently degrades to
# the flat card again, and only inside MyView.
def test_bin_alias_serves_the_real_file(base):
    src = write(base / "model.FBX")
    resp = server.asset_response(FakeRequest(""), base, "model.FBX.bin")
    assert resp._path == src


def test_bin_alias_keeps_the_real_content_type(base):
    write(base / "suit.webp")
    resp = server.asset_response(FakeRequest(""), base, "suit.webp.bin")
    assert resp.headers["Content-Type"] == "image/webp"


def test_bin_alias_still_serves_precompressed(base):
    src = write(base / "model.FBX")
    gz = gz_sibling(src)
    resp = server.asset_response(FakeRequest("gzip"), base, "model.FBX.bin")
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp._path == gz


def test_a_real_bin_file_wins_over_the_alias(base):
    real = write(base / "blob.bin", b"real")
    write(base / "blob", b"decoy")
    resp = server.asset_response(FakeRequest(""), base, "blob.bin")
    assert resp._path == real


def test_bin_alias_cannot_escape_the_root(base):
    from aiohttp import web

    with pytest.raises(web.HTTPNotFound):
        server.asset_response(FakeRequest(""), base, "../server.py.bin")


def test_traj_resync_is_capped(tmp_path, monkeypatch):
    """A re-seed frame must stay small enough for the relay's uplink."""
    import numpy as np

    monkeypatch.setattr(server, "MAP_SAVE", str(tmp_path / "voxel_map.npz"))
    monkeypatch.setattr(server, "lio_session_key", lambda: "http://t6:1/|1")
    hub = server.Hub(voxel=0.1, max_points=1000, min_hits=1)
    for i in range(500):
        hub.map.add_pose(i * 0.5, 0.0, 0.0)

    full = hub.traj_bytes()
    capped = hub.traj_bytes(100)
    assert struct_ntraj(full) == 500
    assert struct_ntraj(capped) == 100
    # Endpoints survive decimation, so the ribbon still spans the whole walk.
    pts = np.frombuffer(capped[16:], np.float32).reshape(-1, 3)
    assert pts[0][0] == pytest.approx(0.0)
    assert pts[-1][0] == pytest.approx(249.5)


def struct_ntraj(payload: bytes) -> int:
    import struct as _struct

    return _struct.unpack("<4sBBHII", payload[:16])[5]
