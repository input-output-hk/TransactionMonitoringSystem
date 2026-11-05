"""How the built SPA is served: index.html revalidates, the rest is untouched.

A browser that keeps a stale index.html after a deploy asks for the previous
build's hashed bundle, which is gone, and shows a blank page until a forced
reload (mainnet, 2026-11-02). The route itself mounts only when the image's
frontend-dist exists, so these drive the handler against a temporary one.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from app.main import _spa_response

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<script src=/assets/index-abc123.js></script>")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside the dist root")
    root = dist.resolve()

    app = FastAPI()

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        return _spa_response(root, full_path)

    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/dashboard", "/attacks/123", "/index.html"])
def test_index_html_is_revalidated_on_every_load(client, path):
    r = client.get(path)

    assert r.status_code == 200
    assert "index-abc123.js" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_other_dist_files_keep_their_default_caching(client):
    r = client.get("/favicon.svg")

    assert r.text == "<svg/>"
    assert "cache-control" not in r.headers


def test_a_path_out_of_the_dist_root_gets_index_html(client):
    r = client.get("/..%2Fsecret.txt")

    assert "outside the dist root" not in r.text
    assert "index-abc123.js" in r.text
