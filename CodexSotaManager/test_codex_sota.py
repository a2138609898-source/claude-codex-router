from __future__ import annotations

import contextlib
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock


CORE_ROOT = Path.home() / "Documents" / "Codex" / "CodexHistorySync"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

import codex_sota_router as router  # noqa: E402
import sota_registry as registry  # noqa: E402


TEST_TOKEN = "unit-test-token"



def make_workspace(registry_module, root: Path):
    """A Workspace whose every path lives under `root`, for patching WORKSPACES in tests.

    secret_path resolves through provider_workspace(provider) now, so redirecting the
    individual module constants is no longer enough — the workspace itself has to move.
    """
    return registry_module.Workspace(
        name="codex",
        label="test",
        root=root,
        router_port=17999,
        router_starter=root / "starter.ps1",
        protocol="responses",
    )

class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict[str, object]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.__class__.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path.split("?", 1)[0] == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-5.6-terra"},
                        {"id": "gpt-5.6-sol"},
                    ],
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        payload = json.loads(body) if body else None
        self.__class__.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "payload": payload,
            }
        )
        self._json(200, {"id": "response_test", "status": "completed"})


class RunningServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "RunningServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def provider_config(base_url: str) -> dict[str, object]:
    return {
        "id": "test_vendor",
        "name": "Test Vendor",
        "base_url": base_url,
        "prefix": "test-vendor--",
        "enabled": True,
        "protected": False,
        "is_default": False,
        "auth_type": "dpapi",
        "secret_file": "test-vendor.dpapi",
        "entropy": "CodexSota.Test.V1",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "models_path": "/models",
        "responses_path": "/responses",
        "timeout_seconds": 5,
        "extra_headers": {},
        "models": [
            {
                "id": "gpt-5.6-sol",
                "enabled": True,
                "display_name": "",
                "description": "",
            }
        ],
    }


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeUpstreamHandler.requests = []

    def test_model_discovery_and_responses_probe(self) -> None:
        with RunningServer(FakeUpstreamHandler) as upstream:
            provider = provider_config(upstream.url + "/v1")
            discovered = registry.discover_models(provider, TEST_TOKEN)
            tested = registry.test_model(
                provider, "gpt-5.6-sol", TEST_TOKEN, reasoning_effort="xhigh"
            )

        self.assertTrue(discovered["ok"])
        self.assertEqual(discovered["models_path"], "/models")
        self.assertEqual(
            discovered["models"], ["gpt-5.6-sol", "gpt-5.6-terra"]
        )
        self.assertTrue(tested["ok"])
        self.assertEqual(FakeUpstreamHandler.requests[0]["path"], "/v1/models")
        self.assertEqual(
            FakeUpstreamHandler.requests[0]["authorization"],
            "Bearer " + TEST_TOKEN,
        )
        response_request = FakeUpstreamHandler.requests[1]
        self.assertEqual(response_request["path"], "/v1/responses")
        self.assertEqual(
            response_request["payload"]["reasoning"]["effort"],  # type: ignore[index]
            "xhigh",
        )


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeUpstreamHandler.requests = []

    def test_exact_route_prefix_stripping_and_accept_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, RunningServer(
            FakeUpstreamHandler
        ) as upstream:
            root = Path(temporary)
            default = provider_config(upstream.url + "/v1")
            default.update(
                {
                    "id": "default_provider",
                    "name": "Default Provider",
                    "prefix": "",
                    "is_default": True,
                    "auth_type": "codex_auth",
                }
            )
            vendor = provider_config(upstream.url + "/v1")
            vendor.update(
                {
                    "id": "vendor",
                    "name": "Vendor",
                    "prefix": "vendor--",
                    "auth_type": "codex_auth",
                }
            )
            registry_path = root / "providers.json"
            registry_path.write_text(
                json.dumps({"version": 1, "providers": [default, vendor]}),
                encoding="utf-8",
            )
            auth_path = root / "auth.json"
            auth_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(router, "read_codex_auth_key", return_value=TEST_TOKEN):
                state = router.RouterState(registry_path, auth_path, root / "router.log")
            handler_server = ThreadingHTTPServer(
                ("127.0.0.1", 0), router.SotaRouterHandler
            )
            handler_server.router_state = state  # type: ignore[attr-defined]
            route_thread = threading.Thread(
                target=handler_server.serve_forever, daemon=True
            )
            route_thread.start()
            route_url = f"http://127.0.0.1:{handler_server.server_port}"
            try:
                selected = urllib.request.Request(
                    route_url + "/v1/responses?trace=1",
                    data=json.dumps(
                        {"model": "vendor--gpt-5.6-sol", "input": "test"}
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                        "Authorization": "Bearer inherited-token",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(selected, timeout=5) as response:
                    self.assertEqual(response.status, 200)

                rejected = urllib.request.Request(
                    route_url + "/responses",
                    data=json.dumps(
                        {"model": "vendor--not-selected", "input": "test"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(rejected, timeout=5)
                self.assertEqual(context.exception.code, 400)
            finally:
                handler_server.shutdown()
                handler_server.server_close()
                route_thread.join(timeout=5)
                state.clear_keys()

        upstream_posts = [
            request
            for request in FakeUpstreamHandler.requests
            if request["method"] == "POST"
        ]
        self.assertEqual(len(upstream_posts), 1)
        self.assertEqual(
            upstream_posts[0]["payload"]["model"],  # type: ignore[index]
            "gpt-5.6-sol",
        )
        self.assertEqual(upstream_posts[0]["path"], "/v1/responses?trace=1")
        self.assertEqual(upstream_posts[0]["accept"], "text/event-stream")
        self.assertEqual(
            upstream_posts[0]["authorization"], "Bearer " + TEST_TOKEN
        )


class TransactionTests(unittest.TestCase):
    def test_apply_failure_restores_registry_catalog_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "providers.json"
            catalog_path = root / "sota-multi-vendor-model-catalog.json"
            secrets_root = root / "secrets"
            lock_path = root / "providers.lock"
            default = provider_config("http://127.0.0.1:1")
            default.update(
                {
                    "id": "default_provider",
                    "name": "Default Provider",
                    "prefix": "",
                    "is_default": True,
                    "auth_type": "codex_auth",
                }
            )
            original_registry = json.dumps(
                {"version": 1, "providers": [default]}, indent=2
            ).encode("utf-8")
            original_catalog = b'{"models":[]}'
            registry_path.write_bytes(original_registry)
            catalog_path.write_bytes(original_catalog)
            candidate = provider_config("http://127.0.0.1:2")

            def load_test_registry(_path: object = None, allow_missing_secrets: bool = False):
                value = json.loads(registry_path.read_text(encoding="utf-8"))
                return registry.validate_registry(value, allow_missing_secrets)

            def write_test_registry(value: dict[str, object], _path: object = None) -> None:
                registry_path.write_text(json.dumps(value), encoding="utf-8")

            def write_test_catalog(*_args: object, **_kwargs: object) -> dict[str, object]:
                catalog_path.write_text('{"changed":true}', encoding="utf-8")
                return {"status": "ok", "models": ["test-vendor--gpt-5.6-sol"]}

            def write_test_secret(
                value: str, path: Path, _entropy: str
            ) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("encrypted:" + str(bool(value)), encoding="ascii")

            patches = [
                mock.patch.dict(
                    registry.WORKSPACES,
                    {"codex": make_workspace(registry, secrets_root.parent)},
                ),
                mock.patch.object(registry, "REGISTRY_PATH", registry_path),
                mock.patch.object(registry, "CATALOG_PATH", catalog_path),
                mock.patch.object(registry, "SECRETS_ROOT", secrets_root),
                mock.patch.object(registry, "REGISTRY_LOCK_PATH", lock_path),
                mock.patch.object(registry, "load_registry", load_test_registry),
                mock.patch.object(registry, "write_registry", write_test_registry),
                mock.patch.object(registry, "build_model_catalog", write_test_catalog),
                mock.patch.object(registry, "dpapi_protect", write_test_secret),
                mock.patch.object(
                    registry, "restart_router", side_effect=RuntimeError("test failure")
                ),
            ]
            # ExitStack rather than indexing: hard-coded indices silently drop the last
            # patch the moment the list grows, which is how the router patch went missing.
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                with self.assertRaisesRegex(RuntimeError, "test failure"):
                    registry.apply_provider(candidate, TEST_TOKEN)

            self.assertEqual(registry_path.read_bytes(), original_registry)
            self.assertEqual(catalog_path.read_bytes(), original_catalog)
            self.assertFalse((secrets_root / "test-vendor.dpapi").exists())

    def test_delete_archives_provider_and_encrypted_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "providers.json"
            catalog_path = root / "sota-multi-vendor-model-catalog.json"
            secrets_root = root / "secrets"
            archive_root = root / "backups" / "deleted-providers"
            lock_path = root / "providers.lock"
            catalog_path.write_text("{}", encoding="utf-8")
            provider = provider_config("http://127.0.0.1:2")
            default = provider_config("http://127.0.0.1:1")
            default.update(
                {
                    "id": "default_provider",
                    "name": "Default Provider",
                    "prefix": "",
                    "is_default": True,
                    "auth_type": "codex_auth",
                }
            )
            value = {"version": 1, "providers": [default, provider]}
            registry_path.write_text(json.dumps(value), encoding="utf-8")
            secrets_root.mkdir()
            secret = secrets_root / "test-vendor.dpapi"
            secret.write_bytes(b"encrypted-test-data")

            def load_test_registry(*_args: object, **_kwargs: object):
                return deepcopy(value)

            def write_test_registry(
                updated: dict[str, object], _path: object = None, **_kwargs: object
            ) -> None:
                registry_path.write_text(json.dumps(updated), encoding="utf-8")

            with mock.patch.dict(
                registry.WORKSPACES, {"codex": make_workspace(registry, secrets_root.parent)}
            ), mock.patch.object(registry, "REGISTRY_PATH", registry_path), mock.patch.object(
                registry, "CATALOG_PATH", catalog_path
            ), mock.patch.object(registry, "SECRETS_ROOT", secrets_root), mock.patch.object(
                registry, "DELETED_PROVIDERS_ROOT", archive_root
            ), mock.patch.object(registry, "REGISTRY_LOCK_PATH", lock_path), mock.patch.object(
                registry, "load_registry", load_test_registry
            ), mock.patch.object(registry, "write_registry", write_test_registry), mock.patch.object(
                registry,
                "build_model_catalog",
                return_value={"status": "ok", "models": ["default"]},
            ), mock.patch.object(
                registry, "restart_router", return_value={"status": "ready"}
            ):
                result = registry.delete_provider("test_vendor")

            archived = Path(result["archive"])
            self.assertFalse(secret.exists())
            self.assertTrue((archived / "provider.json").exists())
            self.assertEqual(
                (archived / "test-vendor.dpapi").read_bytes(),
                b"encrypted-test-data",
            )
            saved = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["id"] for item in saved["providers"]], ["default_provider"]
            )


class CatalogCapabilityTests(unittest.TestCase):
    def test_luna_does_not_inherit_sol_ultra_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            destination = root / "catalog.json"
            levels = ["low", "medium", "high", "xhigh", "max", "ultra"]
            source.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-sol",
                                "supported_reasoning_levels": [
                                    {"effort": effort} for effort in levels
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            test_registry = {
                "version": 1,
                "providers": [
                    {
                        "id": "vendor",
                        "name": "Vendor",
                        "enabled": True,
                        "prefix": "vendor--",
                        "models": [
                            {"id": "gpt-5.6-sol", "enabled": True},
                            {"id": "gpt-5.6-luna", "enabled": True},
                        ],
                    }
                ],
            }

            registry.build_model_catalog(
                registry=test_registry,
                source_path=source,
                destination_path=destination,
            )

            catalog = json.loads(destination.read_text(encoding="utf-8"))
            by_slug = {item["slug"]: item for item in catalog["models"]}
            sol_levels = [
                item["effort"]
                for item in by_slug["vendor--gpt-5.6-sol"][
                    "supported_reasoning_levels"
                ]
            ]
            luna_levels = [
                item["effort"]
                for item in by_slug["vendor--gpt-5.6-luna"][
                    "supported_reasoning_levels"
                ]
            ]
            self.assertIn("ultra", sol_levels)
            self.assertEqual(
                luna_levels, ["low", "medium", "high", "xhigh", "max"]
            )


class ReorderTests(unittest.TestCase):
    """reorder_providers must be transactional: order is what failover walks."""

    def _fixture(self, root: Path) -> tuple[Path, Path, list[dict[str, object]]]:
        default = provider_config("http://127.0.0.1:1")
        default.update(
            {"id": "default_provider", "name": "Default", "prefix": "", "is_default": True,
             "auth_type": "codex_auth"}
        )
        second = provider_config("http://127.0.0.1:2")
        second.update({"id": "vendor_b", "name": "B", "prefix": "b--", "auth_type": "codex_auth"})
        third = provider_config("http://127.0.0.1:3")
        third.update({"id": "vendor_c", "name": "C", "prefix": "c--", "auth_type": "codex_auth"})
        providers = [default, second, third]
        registry_path = root / "providers.json"
        catalog_path = root / "sota-multi-vendor-model-catalog.json"
        registry_path.write_text(
            json.dumps({"version": 1, "providers": providers}), encoding="utf-8"
        )
        catalog_path.write_text('{"models":[]}', encoding="utf-8")
        return registry_path, catalog_path, providers

    def _patches(self, registry_path: Path, catalog_path: Path, root: Path, restart: object):
        def load_test_registry(_path: object = None, allow_missing_secrets: bool = False):
            value = json.loads(registry_path.read_text(encoding="utf-8"))
            return registry.validate_registry(value, allow_missing_secrets)

        return [
            mock.patch.dict(registry.WORKSPACES, {"codex": make_workspace(registry, root)}),
            mock.patch.object(registry, "REGISTRY_PATH", registry_path),
            mock.patch.object(registry, "CATALOG_PATH", catalog_path),
            mock.patch.object(registry, "REGISTRY_LOCK_PATH", root / "providers.lock"),
            mock.patch.object(registry, "load_registry", load_test_registry),
            mock.patch.object(
                registry, "write_registry",
                lambda value, path=registry_path, **_kwargs: path.write_text(
                    json.dumps(value), encoding="utf-8"
                ),
            ),
            mock.patch.object(registry, "build_model_catalog", lambda *_a, **_k: {"models": []}),
            mock.patch.object(registry, "restart_router", restart),
        ]

    def test_reorder_writes_the_new_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path, catalog_path, _ = self._fixture(root)
            wanted = ["vendor_c", "default_provider", "vendor_b"]
            with contextlib.ExitStack() as stack:
                for patch in self._patches(
                    registry_path, catalog_path, root, lambda *_a, **_k: {"status": "ok"}
                ):
                    stack.enter_context(patch)
                registry.reorder_providers(wanted)
            saved = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in saved["providers"]], wanted)

    def test_reorder_rejects_an_incomplete_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path, catalog_path, _ = self._fixture(root)
            before = registry_path.read_bytes()
            with contextlib.ExitStack() as stack:
                for patch in self._patches(
                    registry_path, catalog_path, root, lambda *_a, **_k: {"status": "ok"}
                ):
                    stack.enter_context(patch)
                with self.assertRaisesRegex(ValueError, "every existing provider"):
                    registry.reorder_providers(["vendor_c", "default_provider"])
            self.assertEqual(registry_path.read_bytes(), before)

    def test_reorder_rolls_back_when_the_router_will_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path, catalog_path, _ = self._fixture(root)
            before = registry_path.read_bytes()

            def restart(*_args: object, **_kwargs: object) -> dict[str, object]:
                raise RuntimeError("router refused")

            with contextlib.ExitStack() as stack:
                for patch in self._patches(registry_path, catalog_path, root, restart):
                    stack.enter_context(patch)
                with self.assertRaisesRegex(RuntimeError, "router refused"):
                    registry.reorder_providers(["vendor_c", "vendor_b", "default_provider"])
            self.assertEqual(registry_path.read_bytes(), before)


class LayoutTests(unittest.TestCase):
    """Guards against widgets landing in a grid cell that is already taken.

    Tk silently stacks them instead of complaining, so the only symptom is text painted
    over the buttons underneath — which no behavioural assertion would ever catch.
    """

    def test_layout_is_sane(self) -> None:
        """One Tk root, three layout invariants.

        Kept as a single test because this environment only lets one Tk root be created per
        process — a second CodexSotaApp() fails with "Can't find a usable init.tcl", which
        silently turned the second layout test into a skip.

        Checks: no two widgets share a grid cell; every control a user must click is mapped
        with a real height; and the sidebar's action rows sit fully inside the sidebar rather
        than being pushed past its bottom edge by the expanding provider list.
        """
        try:
            import tkinter
        except ImportError:  # pragma: no cover - headless build
            self.skipTest("tkinter is unavailable")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import CodexSotaManager as manager

        def clashes_in(container: object) -> dict[tuple[int, int], list[str]]:
            occupied: dict[tuple[int, int], list[str]] = {}
            for child in container.winfo_children():  # type: ignore[attr-defined]
                info = child.grid_info()
                if not info:
                    continue
                row, col = int(info["row"]), int(info["column"])
                for r in range(row, row + int(info["rowspan"])):
                    for c in range(col, col + int(info["columnspan"])):
                        occupied.setdefault((r, c), []).append(str(child))
            return {cell: names for cell, names in occupied.items() if len(names) > 1}

        def walk(container: object) -> list[str]:
            problems = []
            found = clashes_in(container)
            if found:
                problems.append(f"{container} 有控件挤在同一个 grid 格子里: {found}")
            for child in container.winfo_children():  # type: ignore[attr-defined]
                problems.extend(walk(child))
            return problems

        with mock.patch.object(manager.messagebox, "showerror"),                 mock.patch.object(manager.messagebox, "showinfo"),                 mock.patch.object(manager.messagebox, "showwarning"):
            try:
                app = manager.CodexSotaApp()
            except tkinter.TclError as error:  # pragma: no cover - no display
                self.skipTest(f"no display: {error}")
            try:
                app.update()
                app.update_idletasks()

                self.assertEqual(walk(app), [])

                must_click = (
                    "add_button", "delete_button", "move_up_button", "move_down_button",
                    "save_button", "reload_button", "launch_button", "audit_button",
                    "provider_tree", "provider_filter_entry",
                )
                invisible = []
                for name in must_click:
                    widget = getattr(app, name, None)
                    if widget is None:
                        invisible.append(f"{name}=缺失")
                    elif not widget.winfo_ismapped() or widget.winfo_height() <= 1:
                        invisible.append(f"{name}(h={widget.winfo_height()})")
                self.assertEqual(invisible, [], f"这些控件看不到: {invisible}")

                # Squeeze the window below its own minsize: at the default size there is
                # slack, so a wrong packing order looks fine. The bug only shows when space
                # is tight, which is exactly when the user notices it.
                sidebar = app.add_button.master.master
                app.minsize(1, 1)
                for height in (560, 480):
                    app.geometry(f"1220x{height}")
                    app.update()
                    app.update_idletasks()
                    overflow = []
                    for name in (
                        "move_up_button", "move_down_button", "add_button", "delete_button"
                    ):
                        widget = getattr(app, name)
                        top = widget.winfo_rooty() - sidebar.winfo_rooty()
                        bottom = top + widget.winfo_height()
                        if not widget.winfo_ismapped() or bottom > sidebar.winfo_height():
                            overflow.append(
                                f"{name}(底={bottom} 侧栏高={sidebar.winfo_height()} "
                                f"mapped={widget.winfo_ismapped()})"
                            )
                    self.assertEqual(
                        overflow, [], f"窗口高 {height} 时侧栏按钮被挤出可见区域: {overflow}"
                    )
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
