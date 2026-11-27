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
