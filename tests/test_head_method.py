# tests/test_head_method.py
"""HEAD-запросы (проверки доступности, предпросмотр ссылок) отвечают как GET, но без тела.

Раньше любой HEAD получал 405 «Method Not Allowed»: бесплатные мониторы вроде UptimeRobot
шлют именно HEAD и считали бы сайт недоступным."""


def test_head_answers_like_get_without_body(client):
    got = client.get("/login")
    head = client.head("/login")
    assert got.status_code == 200
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"] == got.headers["content-type"]
    assert "content-security-policy" in head.headers          # защитные заголовки на месте


def test_head_works_on_main_public_pages(client):
    for path in ("/", "/catalog", "/deadlines", "/register", "/robots.txt", "/sitemap.xml"):
        resp = client.head(path)
        assert resp.status_code == 200, path
        assert resp.content == b"", path


def test_head_keeps_errors_and_redirects(client):
    assert client.head("/no-such-page-xyz").status_code == 404
    resp = client.head("/admin", follow_redirects=False)       # без входа — на страницу входа
    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers["location"]


def test_head_does_not_run_post_only_routes(client):
    # POST-адрес по-прежнему недоступен для чтения: HEAD → GET → 405, обмен не запускается
    assert client.head("/api/1c/supplies/sync").status_code == 405
