"""First-run default app for the desktop launcher (pure Python, no Cocoa)."""
import json

from tuomin_gateway.shell.defaults import DEFAULT_APP_ID, ensure_default_workbench_app


def test_first_run_registers_workbench_with_an_empty_dictionary(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text('{"apps": {}}\n', encoding="utf-8")

    assert ensure_default_workbench_app(config, tmp_path) is True
    registry = json.loads(config.read_text(encoding="utf-8"))
    entry = registry["apps"][DEFAULT_APP_ID]
    assert entry["profile"] == "file_workbench"
    assert entry["name"]
    dictionary = tmp_path / "dictionaries" / "workbench.json"
    assert entry["dictionary"] == str(dictionary)
    assert json.loads(dictionary.read_text(encoding="utf-8")) == []
    from tests.permission_assertions import assert_owner_private
    assert_owner_private(dictionary, is_dir=False)
    assert_owner_private(config, is_dir=False)


def test_rerun_is_idempotent_and_never_rewrites_user_changes(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text('{"apps": {}}\n', encoding="utf-8")
    ensure_default_workbench_app(config, tmp_path)
    registry = json.loads(config.read_text(encoding="utf-8"))
    registry["apps"][DEFAULT_APP_ID]["name"] = "我改过的名字"
    registry["apps"]["other"] = {"profile": "strict"}
    config.write_text(json.dumps(registry), encoding="utf-8")

    assert ensure_default_workbench_app(config, tmp_path) is False
    after = json.loads(config.read_text(encoding="utf-8"))
    assert after["apps"][DEFAULT_APP_ID]["name"] == "我改过的名字"
    assert "other" in after["apps"]


def test_missing_dictionary_file_is_recreated_without_touching_the_registry(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text('{"apps": {}}\n', encoding="utf-8")
    ensure_default_workbench_app(config, tmp_path)
    dictionary = tmp_path / "dictionaries" / "workbench.json"
    dictionary.unlink()
    before = config.read_text(encoding="utf-8")

    assert ensure_default_workbench_app(config, tmp_path) is False
    assert dictionary.exists()
    assert config.read_text(encoding="utf-8") == before


def test_unreadable_or_foreign_registry_is_left_alone(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text("not json", encoding="utf-8")
    assert ensure_default_workbench_app(config, tmp_path) is False
    assert config.read_text(encoding="utf-8") == "not json"
    assert not (tmp_path / "dictionaries").exists()


def test_public_lite_defaults_require_bundle_marker_and_preserve_strict(tmp_path, monkeypatch):
    import sys
    from tuomin_gateway.profiles import RULES_WORKBENCH, get_profile
    from tuomin_gateway.shell.defaults import is_public_lite_build

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not is_public_lite_build()
    (tmp_path / "public-lite.txt").touch()
    assert is_public_lite_build()
    registry = tmp_path / "apps.json"
    ensure_default_workbench_app(registry, tmp_path)
    payload = json.loads(registry.read_text())
    assert payload["apps"][DEFAULT_APP_ID]["profile"] == RULES_WORKBENCH
    assert not get_profile(RULES_WORKBENCH).use_ner
    assert get_profile("file_workbench").use_ner
    payload["apps"][DEFAULT_APP_ID]["profile"] = "strict"
    registry.write_text(json.dumps(payload))
    assert not ensure_default_workbench_app(registry, tmp_path)
    assert json.loads(registry.read_text()) == payload
    monkeypatch.setattr(sys, "frozen", False)
    assert not is_public_lite_build()


def test_public_lite_clipboard_follows_workbench_but_retains_explicit_policy(tmp_path, monkeypatch):
    from tuomin_gateway.profiles import RULES_WORKBENCH
    from tuomin_gateway.shell import registration

    class Admin:
        entries = {"workbench": {"profile": RULES_WORKBENCH, "dictionary": "dictionaries/workbench.json"}}

        def request(self, method, path, payload, **_):
            if path == "/admin/apps":
                return 200, {"apps": [{"app_id": key, **entry} for key, entry in self.entries.items()]}
            app_id = path.rsplit("/", 1)[-1]
            if method == "PUT":
                self.entries[app_id] = payload
            return 200, {"entry": self.entries.get(app_id, {})}

    monkeypatch.setattr(registration, "is_public_lite_build", lambda: True)
    client = Admin()
    clipboard = registration.ClipboardRegistration(client, "test-admin", tmp_path)
    clipboard.configure("workbench")
    assert client.entries["clipboard"]["profile"] == RULES_WORKBENCH
    assert client.entries["clipboard"]["dictionary"] == client.entries["workbench"]["dictionary"]
    client.entries["clipboard"]["profile"] = "strict"
    clipboard.configure("workbench")
    assert client.entries["clipboard"]["profile"] == "strict"
