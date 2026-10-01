"""配置：默认值、文件覆盖、环境变量覆盖、非法配置报错。"""

from __future__ import annotations

import pytest

from pdf2epub.config import Config, load_config
from pdf2epub.errors import ConfigError


def test_defaults():
    config = load_config(None, cwd="/nonexistent-path-for-defaults")
    assert config.workdir == ".pdf2epub"
    assert config.mineru.model_version == "vlm"
    assert config.calibrate.critical_score < config.calibrate.score_threshold
    assert config.validate.fail_on_severity == "ERROR"


def test_file_overrides(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        """
workdir = "out"
[calibrate]
score_threshold = 0.9
[validate]
fail_on_severity = "WARNING"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.workdir == "out"
    assert config.calibrate.score_threshold == 0.9
    assert config.validate.fail_on_severity == "WARNING"
    # 未覆盖的字段保留默认值
    assert config.calibrate.max_segments == 200


def test_template_toml_is_valid():
    """仓库自带的 pdf2epub.toml 必须能被加载——它是用户的第一份参考。"""
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / "pdf2epub.toml"
    config = load_config(template)
    assert config.mineru.model_version == "vlm"
    assert config.validate.fail_on_severity == "ERROR"


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[validate]\nnot_a_field = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not_a_field"):
        load_config(path)


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF2EPUB__WORKDIR", "from-env")
    monkeypatch.setenv("PDF2EPUB__CALIBRATE__MAX_SEGMENTS", "7")
    monkeypatch.setenv("PDF2EPUB__COMPOSE__SCAFFOLD_SAMPLE", "false")
    config = load_config(None, cwd=tmp_path)
    assert config.workdir == "from-env"
    assert config.calibrate.max_segments == 7
    assert config.compose.scaffold_sample is False


def test_invalid_thresholds_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[calibrate]\ncritical_score = 0.9\nscore_threshold = 0.3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="critical_score"):
        load_config(path)


def test_invalid_enum_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[validate]\nfail_on_severity = "NOPE"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="fail_on_severity"):
        load_config(path)


def test_token_resolution(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc")
    config = Config()
    config.mineru.token_env = "MY_TOKEN"
    assert config.mineru.resolve_token() == "abc"
    config.mineru.token = "explicit"
    assert config.mineru.resolve_token() == "explicit"


def test_dotted_overrides():
    config = load_config(None, overrides={"calibrate.max_segments": 3})
    assert config.calibrate.max_segments == 3
