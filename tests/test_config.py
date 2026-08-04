"""配置文件加载测试。"""

from __future__ import annotations

from pathlib import Path

from relaynote.config import Settings


def test_settings_loads_from_config_file(tmp_path: Path) -> None:
    """验证全部设置从 config.toml 读取，不依赖环境变量。"""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[api]
key = "test-key"
base_url = "http://127.0.0.1:9000/v1"
model = "test-model"

[paths]
data_dir = "~/test/data"
workspaces = "~/test/workspaces"
codex_path = "/usr/bin/codex"
expected_codex_version = "codex-cli 0.999.0"
shell = "/bin/zsh"
""",
        encoding="utf-8",
    )
    settings = Settings.from_config(path)
    assert settings.deepseek_api_key == "test-key"
    assert settings.deepseek_base_url == "http://127.0.0.1:9000/v1"
    assert settings.deepseek_model == "test-model"
    assert settings.data_dir == Path("~/test/data").expanduser().resolve()
    assert settings.managed_root == Path("~/test/workspaces").expanduser().resolve()
    assert settings.codex_path == Path("/usr/bin/codex")
    assert settings.expected_codex_version == "codex-cli 0.999.0"
    assert settings.shell_path == Path("/bin/zsh").resolve()


def test_missing_config_creates_template(tmp_path: Path) -> None:
    """验证缺失配置文件时生成默认模板。"""
    path = tmp_path / "config.toml"
    settings = Settings.from_config(path)
    assert path.is_file()
    assert settings.deepseek_api_key is None
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert path.stat().st_mode & 0o777 == 0o600
