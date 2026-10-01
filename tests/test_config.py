"""配置：默认值、文件覆盖、环境变量覆盖、非法配置报错。"""

from __future__ import annotations

import os

import pytest

from pdf2epub.config import Config, load_config, load_env_file
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


class TestEnvFile:
    """``.env`` 支持：项目里没有 dotenv 依赖，这一小块得自己兜住。"""

    def _write(self, tmp_path, content: str):
        (tmp_path / ".env").write_text(content, encoding="utf-8")

    def test_loads_pairs_and_ignores_noise(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDF2EPUB_TEST_ALPHA", raising=False)
        monkeypatch.delenv("PDF2EPUB_TEST_BETA", raising=False)
        monkeypatch.delenv("PDF2EPUB_TEST_GAMMA", raising=False)
        self._write(
            tmp_path,
            "# 注释\n\n"
            "PDF2EPUB_TEST_ALPHA=plain\n"
            'PDF2EPUB_TEST_BETA="有引号"\n'
            "export PDF2EPUB_TEST_GAMMA='单引号'\n"
            "这行没有等号\n",
        )
        assert load_env_file(cwd=tmp_path) == tmp_path / ".env"
        assert os.environ["PDF2EPUB_TEST_ALPHA"] == "plain"
        assert os.environ["PDF2EPUB_TEST_BETA"] == "有引号"
        assert os.environ["PDF2EPUB_TEST_GAMMA"] == "单引号"

    def test_real_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PDF2EPUB_TEST_KEEP", "来自环境")
        self._write(tmp_path, "PDF2EPUB_TEST_KEEP=来自文件\n")
        load_env_file(cwd=tmp_path)
        assert os.environ["PDF2EPUB_TEST_KEEP"] == "来自环境"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_env_file(cwd=tmp_path) is None

    def test_values_are_literal(self, tmp_path, monkeypatch):
        """secret 里出现 $ 或 % 不该被吃掉或展开。"""
        monkeypatch.delenv("PDF2EPUB_TEST_SECRET", raising=False)
        (tmp_path / ".env").write_text("PDF2EPUB_TEST_SECRET=a$b%TEMP%c\n", encoding="utf-8")
        load_env_file(cwd=tmp_path)
        assert os.environ["PDF2EPUB_TEST_SECRET"] == "a$b%TEMP%c"

    def test_load_config_picks_up_env_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDF2EPUB_TEST_TOKEN", raising=False)
        self._write(tmp_path, "PDF2EPUB_TEST_TOKEN=tok\nPDF2EPUB__CALIBRATE__MAX_SEGMENTS=9\n")
        config = load_config(None, cwd=tmp_path)
        assert config.env_path == str(tmp_path / ".env")
        assert os.environ["PDF2EPUB_TEST_TOKEN"] == "tok"
        assert config.calibrate.max_segments == 9

    def test_token_resolves_from_env_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MINERU_TOKEN", raising=False)
        self._write(tmp_path, "MINERU_TOKEN=sk-from-dotenv\n")
        config = load_config(None, cwd=tmp_path)
        assert config.mineru.resolve_token() == "sk-from-dotenv"


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
