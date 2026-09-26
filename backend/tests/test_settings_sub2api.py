from app.api import settings as settings_api
from app.config import settings


def test_settings_exposes_default_sub2api_group_ids(monkeypatch):
    monkeypatch.setattr(settings, "sub2api_group_ids", "42, 108")

    result = settings_api.get_settings()

    assert result.sub2api_group_ids == "42, 108"


def test_update_settings_persists_and_clears_default_sub2api_group_ids(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "sub2api_group_ids", "")

    settings_api.update_settings({"sub2api_group_ids": "42, 108"})
    assert settings.sub2api_group_ids == "42, 108"
    assert "SUB2API_GROUP_IDS=42, 108" in env_file.read_text(encoding="utf-8")

    settings_api.update_settings({"sub2api_group_ids": ""})
    assert settings.sub2api_group_ids == ""
    assert "SUB2API_GROUP_IDS=\n" in env_file.read_text(encoding="utf-8")


def test_settings_reports_effective_sub2api_proxy(tmp_path, monkeypatch):
    """未显式配置代理时，Sub2API 出口回退到 DEFAULT_PROXY（区域准入 403 的修复点）。"""
    monkeypatch.setattr(settings, "sub2api_proxy", "")
    monkeypatch.setattr(settings, "default_proxy", "http://127.0.0.1:7890")

    result = settings_api.get_settings()

    assert result.sub2api_proxy == ""
    assert result.sub2api_proxy_effective == "http://127.0.0.1:7890"


def test_update_settings_persists_and_clears_sub2api_proxy(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "sub2api_proxy", "http://127.0.0.1:7890")

    settings_api.update_settings({"sub2api_proxy": "direct"})
    assert settings.sub2api_proxy == "direct"
    assert "SUB2API_PROXY=direct" in env_file.read_text(encoding="utf-8")
    assert settings_api.get_settings().sub2api_proxy_effective == ""

    settings_api.update_settings({"sub2api_proxy": ""})
    assert settings.sub2api_proxy == ""
    assert "SUB2API_PROXY=\n" in env_file.read_text(encoding="utf-8")
