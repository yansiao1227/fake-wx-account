"""Regression tests for untrusted image results; all networking is synthetic."""

import base64
import importlib.util
import io
import ipaddress
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


def image_bytes(format):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format=format)
    return buffer.getvalue()


PNG = image_bytes("PNG")
PUBLIC_URL = "https://cdn.synthetic.invalid/image.png?signature=synthetic"


@pytest.fixture
def generate(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "skills/image-generation/scripts/generate.py"
    spec = importlib.util.spec_from_file_location("synthetic_image_download", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def resolve(host, port, **_kwargs):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = ipaddress.ip_address("93.184.216.34")
        family = module.socket.AF_INET6 if address.version == 6 else module.socket.AF_INET
        endpoint = (str(address), port, 0, 0) if address.version == 6 else (str(address), port)
        return [(family, module.socket.SOCK_STREAM, 6, "", endpoint)]

    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.socket, "socket", lambda *_args: pytest.fail("unmocked image socket"))
    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: pytest.fail("unmocked API request"))
    return module


def fake_connections(generate, monkeypatch, responses):
    calls = []
    pending = iter(responses)

    class Connection:
        def __init__(self, host, port, **kwargs):
            self.call = {"host": host, "port": port, **kwargs}
            calls.append(self.call)

        def request(self, method, target, **kwargs):
            self.call.update(method=method, target=target, **kwargs)

        def getresponse(self):
            status, location, body = next(pending)
            return SimpleNamespace(status=status, getheader=lambda _name: location, read=lambda: body)

        def close(self):
            self.call["closed"] = True

    monkeypatch.setattr(generate, "_PublicImageHTTPConnection", Connection)
    monkeypatch.setattr(generate, "_PublicImageHTTPSConnection", Connection)
    return calls


@pytest.mark.parametrize("url,port,target", [
    (PUBLIC_URL, 443, "/image.png?signature=synthetic"),
    ("http://cdn.synthetic.invalid:8080/", 8080, "/"),
])
def test_public_download_has_no_api_authorization(generate, monkeypatch, url, port, target):
    calls = fake_connections(generate, monkeypatch, [(200, None, PNG)])
    assert generate._download_image(url) == PNG
    assert len(calls) == 1
    assert calls[0]["port"] == port and calls[0]["target"] == target
    assert calls[0]["method"] == "GET" and calls[0]["headers"] == {"Accept": "image/*"}
    assert calls[0]["timeout"] == 60 and calls[0]["closed"]


@pytest.mark.parametrize("host", [
    "0.0.0.0", "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "100.64.0.1", "224.0.0.1", "240.0.0.1",
    "::", "::1", "fc00::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1",
])
def test_nonpublic_result_url_is_rejected_before_http(generate, monkeypatch, host):
    calls = fake_connections(generate, monkeypatch, [])
    authority = f"[{host}]" if ":" in host else host
    with pytest.raises(RuntimeError, match="public Internet"):
        generate._download_image(f"http://{authority}/image.png")
    assert calls == []


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_public_and_private_dns_is_rejected(generate, monkeypatch, reverse):
    records = [(generate.socket.AF_INET, generate.socket.SOCK_STREAM, 6, "", (ip, 443))
               for ip in ("93.184.216.34", "127.0.0.1")]
    monkeypatch.setattr(generate.socket, "getaddrinfo", lambda *_args, **_kwargs: records[::-1] if reverse else records)
    calls = fake_connections(generate, monkeypatch, [])
    with pytest.raises(RuntimeError, match="public Internet"):
        generate._download_image(PUBLIC_URL)
    assert calls == []


@pytest.mark.parametrize("source", [
    "file:///C:/Users/example/private.png", "ftp://cdn.synthetic.invalid/image.png",
    "data:image/png;base64,synthetic", "C:\\Users\\example\\private.png",
    "\\\\server\\share\\private.png", "/home/example/private.png", "//server/image.png",
    "https://user:password@cdn.synthetic.invalid/image.png", "https://cdn.synthetic.invalid:bad/image.png",
    "http://[fe80::1%25eth0]/image.png", " https://cdn.synthetic.invalid/image.png", None,
])
def test_result_must_be_an_http_url(generate, monkeypatch, source):
    calls = fake_connections(generate, monkeypatch, [])
    with pytest.raises(RuntimeError, match="HTTP or HTTPS"):
        generate._download_image(source)
    assert calls == []


def test_dns_without_records_is_rejected(generate, monkeypatch):
    monkeypatch.setattr(generate.socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(RuntimeError, match="no resolved address"):
        generate._download_image(PUBLIC_URL)


@pytest.mark.parametrize("location", ["http://127.0.0.1/private.png", "file:///C:/private.png"])
def test_redirect_target_is_validated_before_another_request(generate, monkeypatch, location):
    calls = fake_connections(generate, monkeypatch, [(302, location, b"")])
    with pytest.raises(RuntimeError):
        generate._download_image(PUBLIC_URL)
    assert len(calls) == 1 and calls[0]["closed"]


def test_relative_public_redirect_is_downloaded(generate, monkeypatch):
    calls = fake_connections(generate, monkeypatch, [(307, "/final.webp", b""), (200, None, PNG)])
    assert generate._download_image(PUBLIC_URL) == PNG
    assert [call["target"] for call in calls] == ["/image.png?signature=synthetic", "/final.webp"]
    assert all(call["closed"] for call in calls)


def test_redirect_loop_is_bounded(generate, monkeypatch):
    calls = fake_connections(generate, monkeypatch, [(302, PUBLIC_URL, b"")] * 6)
    with pytest.raises(RuntimeError, match="excessive redirect"):
        generate._download_image(PUBLIC_URL)
    assert len(calls) == 6 and all(call["closed"] for call in calls)


@pytest.mark.parametrize("status,location", [(302, None), (403, None)])
def test_failed_download_closes_connection(generate, monkeypatch, status, location):
    calls = fake_connections(generate, monkeypatch, [(status, location, b"")])
    with pytest.raises(RuntimeError):
        generate._download_image(PUBLIC_URL)
    assert calls[0]["closed"]


@pytest.mark.parametrize("secure", [False, True])
def test_connection_pins_validated_ip_and_keeps_tls_hostname(generate, monkeypatch, secure):
    resolutions = []

    def changing_dns(host, port, **_kwargs):
        resolutions.append(host)
        ip = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(generate.socket.AF_INET, generate.socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(generate.socket, "getaddrinfo", changing_dns)
    connected = []
    tls_names = []
    sent = []
    sock = SimpleNamespace(settimeout=lambda _timeout: None, connect=lambda endpoint: connected.append(endpoint),
                           close=lambda: None, sendall=lambda data: sent.append(data))
    monkeypatch.setattr(generate.socket, "socket", lambda *_args: sock)
    connection_type = generate._PublicImageHTTPSConnection if secure else generate._PublicImageHTTPConnection
    port = 443 if secure else 80
    addresses = generate._public_image_addresses("cdn.synthetic.invalid", port)
    connection = connection_type("cdn.synthetic.invalid", port, addresses=addresses, timeout=60)
    if secure:
        assert connection._context.check_hostname
        assert connection._context.verify_mode == ssl.CERT_REQUIRED
        connection._context = SimpleNamespace(wrap_socket=lambda raw, server_hostname:
                                             tls_names.append(server_hostname) or raw)
    connection.connect()
    connection.request("GET", "/image.png", headers={"Accept": "image/*"})
    assert connected == [("93.184.216.34", port)] and resolutions == ["cdn.synthetic.invalid"]
    assert connection.host == "cdn.synthetic.invalid"
    assert tls_names == (["cdn.synthetic.invalid"] if secure else [])
    assert b"Host: cdn.synthetic.invalid\r\n" in b"".join(sent)
    connection.close()


@pytest.mark.parametrize("label,payload", [
    ("OpenAIProvider", lambda path: {"data": [{"url": path}]}),
    ("LinkAIProvider", lambda path: {"data": [{"url": path}]}),
    ("SeedreamProvider", lambda path: {"data": [{"url": path}]}),
    ("QwenProvider", lambda path: {"output": {"choices": [{"message": {"content": [{"image": path}]}}]}}),
    ("MinimaxProvider", lambda path: {"data": {"image_urls": [path]}}),
])
def test_api_result_cannot_read_a_local_image(generate, monkeypatch, tmp_path, label, payload):
    private = tmp_path / "private.png"
    private.write_bytes(PNG)
    output = tmp_path / "generated"
    api_result = payload(str(private))
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs:
                        SimpleNamespace(status_code=200, json=lambda: api_result))
    provider = getattr(generate, label)("synthetic-key", "https://api.synthetic.invalid", "")
    with pytest.raises(RuntimeError, match="HTTP or HTTPS"):
        provider.generate("synthetic prompt", output_dir=str(output))
    assert not output.exists()
    assert generate._load_image(str(private)) == PNG


@pytest.mark.parametrize("format,extension,mime", [("PNG", ".png", "image/png"),
                                                 ("JPEG", ".jpg", "image/jpeg"),
                                                 ("WEBP", ".webp", "image/webp")])
def test_saved_image_format_matches_artifact_mime(generate, tmp_path, format, extension, mime):
    from agent.tools.utils.image_artifacts import build_file_to_send

    raw = image_bytes(format)
    provider = generate.OpenAIProvider("synthetic-key", "https://api.synthetic.invalid", "")
    paths = provider._save_results({"data": [{"b64_json": base64.b64encode(raw).decode()}]}, str(tmp_path))
    assert Path(paths[0]).suffix == extension and Path(paths[0]).read_bytes() == raw
    assert build_file_to_send(paths[0])["mime_type"] == mime


@pytest.mark.parametrize("format", ["GIF", "BMP", "TIFF"])
def test_unsupported_image_formats_are_not_saved(generate, tmp_path, format):
    provider = generate.OpenAIProvider("synthetic-key", "https://api.synthetic.invalid", "")
    with pytest.raises(RuntimeError, match="unsupported image content"):
        provider._save_results({"data": [{"b64_json": base64.b64encode(image_bytes(format)).decode()}]}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", [{}, {"b64_json": "invalid"}, {"b64_json": base64.b64encode(PNG[:20]).decode()}])
def test_later_invalid_response_does_not_leave_first_image(generate, tmp_path, invalid):
    existing = tmp_path / "existing.png"
    existing.write_bytes(PNG)
    provider = generate.OpenAIProvider("synthetic-key", "https://api.synthetic.invalid", "")
    with pytest.raises(RuntimeError):
        provider._save_results({"data": [{"b64_json": base64.b64encode(PNG).decode()}, invalid]}, str(tmp_path))
    assert list(tmp_path.iterdir()) == [existing]


def test_save_failure_removes_this_batches_completed_images(generate, monkeypatch, tmp_path):
    original_save = generate._save_image
    saves = []

    def fail_second(data, output_dir):
        if saves:
            raise OSError("synthetic disk failure")
        path = original_save(data, output_dir)
        saves.append(path)
        return path

    monkeypatch.setattr(generate, "_save_image", fail_second)
    provider = generate.OpenAIProvider("synthetic-key", "https://api.synthetic.invalid", "")
    item = {"b64_json": base64.b64encode(PNG).decode()}
    with pytest.raises(RuntimeError, match="synthetic disk failure"):
        provider._save_results({"data": [item, item]}, str(tmp_path))
    assert saves and list(tmp_path.iterdir()) == []


def test_partial_file_write_failure_removes_incomplete_image(generate, monkeypatch, tmp_path):
    original_open = open

    class FailedWriter:
        def __init__(self, path, mode):
            self.file = original_open(path, mode)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.file.close()

        def write(self, data):
            self.file.write(data[:8])
            raise OSError("synthetic partial write failure")

    monkeypatch.setattr(generate, "open", FailedWriter, raising=False)
    with pytest.raises(OSError, match="partial write failure"):
        generate._save_image(PNG, str(tmp_path))
    assert list(tmp_path.iterdir()) == []
