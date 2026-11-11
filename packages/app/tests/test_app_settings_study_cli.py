"""模型配置（config.toml / 环境变量）与 examprep study 命令。"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from conftest import SAMPLES
from fakes import Brain

from examprep import cli
from examprep.settings import SettingsError, load_settings
from examprep_llm import Gateway
from examprep_llm.testing import FakeClock, FakeProvider

# ---------------------------------------------------------------- 配置


def test_env_key_defaults(tmp_path: Path) -> None:
    cfg = load_settings(tmp_path, {"DEEPSEEK_API_KEY": "sk"})
    assert cfg.roles["tutor"] == ["deepseek/deepseek-flash"]
    assert set(cfg.roles) == {"tutor", "grader", "verifier"}
    cfg = load_settings(tmp_path, {"ANTHROPIC_API_KEY": "a"})
    assert cfg.roles["grader"] == ["anthropic/claude-haiku-4-5"]
    assert cfg.reasoning == {"tutor": "high", "grader": "high", "verifier": "high"}  # 默认都开思考
    cfg = load_settings(tmp_path, {"EXAMPREP_OLLAMA_MODEL": "qwen3:8b"})
    assert cfg.roles["verifier"] == ["ollama/qwen3:8b"]
    assert cfg.reasoning == {}  # 本地模型不发送思考参数
    with pytest.raises(SettingsError, match="还没有配置模型"):
        load_settings(tmp_path, {})


def test_config_file(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[providers.myvllm]\nprotocol = "openai"\nbase_url = "http://gpu:8000/v1"\n\n[roles]\ntutor = ["myvllm/qwen"]\ngrader = ["myvllm/small"]\n',
        encoding="utf-8",
    )
    cfg = load_settings(tmp_path, {"DEEPSEEK_API_KEY": "ignored"})
    assert cfg.roles == {"tutor": ["myvllm/qwen"], "grader": ["myvllm/small"], "verifier": ["myvllm/qwen"]}
    assert cfg.providers["myvllm"]["base_url"] == "http://gpu:8000/v1"
    assert cfg.reasoning["tutor"] == "high"
    (tmp_path / "config.toml").write_text(
        '[roles]\ntutor = ["deepseek/deepseek-flash"]\n[reasoning]\ntutor = "low"\n', encoding="utf-8"
    )
    assert load_settings(tmp_path, {}).reasoning == {"tutor": "low", "grader": "high", "verifier": "high"}
    (tmp_path / "config.toml").write_text(
        '[roles]\ntutor = ["deepseek/deepseek-flash"]\n[reasoning]\ntutor = "very"\n', encoding="utf-8"
    )
    with pytest.raises(SettingsError, match="配置无效"):
        load_settings(tmp_path, {})
    (tmp_path / "config.toml").write_text("[roles]\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="缺少"):
        load_settings(tmp_path, {})
    (tmp_path / "config.toml").write_text("not toml = = 1", encoding="utf-8")
    with pytest.raises(SettingsError, match="格式错误"):
        load_settings(tmp_path, {})
    (tmp_path / "config.toml").write_text('[roles]\ntutor = ["x/y"]\nbogus_section = 1\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="配置无效"):
        load_settings(tmp_path, {})


# ---------------------------------------------------------------- study 命令


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "数据结构"
    shutil.copytree(SAMPLES / "zh-data-structures", root)
    d = tmp_path / "data"
    assert cli.main(["--data-dir", str(d), "ingest", str(root), "--quiet"], out=io.StringIO()) == 0
    return d


def scripted(*lines: str) -> object:
    it = iter([*(f"{x}\n" for x in lines)])
    return lambda: next(it, "")


def fake_gateway() -> Gateway:
    return Gateway(
        {"fake": FakeProvider(responder=Brain())},
        {r: ["fake/m"] for r in ("tutor", "grader", "verifier")},
        clock=FakeClock(),
    )


def test_study_session_end_to_end(data: Path) -> None:
    out = io.StringIO()
    rc = cli.main(
        ["--data-dir", str(data), "study", "数据结构", "--mode", "longterm"],
        out=out,
        read_line=scripted(
            "继续", "懂了，后进先出", "继续", "B", "看答案", "量子纠缠是什么？", "进度", "退出"
        ),  # type: ignore[arg-type]
        gateway=fake_gateway(),
    )
    text = out.getvalue()
    assert rc == 0
    assert "开始学习：数据结构" in text
    assert "📚 数据结构（长期模式" in text
    assert "（第1章_线性表.md p.1）" in text  # 系统生成的引用
    assert "已删除 1 个资料中不存在的引用" in text
    assert "句在资料中找不到依据（已标 🟡）" in text
    assert "📝 第 1/2 题 [q006]" in text
    assert "✅ 答对了（规则判分）" in text
    assert "⏭ 已跳过" in text
    assert "参考答案（期末模拟卷.txt p.1）" in text
    assert "🏁 第 1 章完成：已验证" in text
    assert "资料里没有讲到这一点。" in text
    assert "下一步：" in text
    assert "进度已保存" in text

    # 第二次进入：接着学（第 2 章）；--reasoning 只改讲解的思考强度
    out2 = io.StringIO()
    gw = fake_gateway()
    cli.main(
        ["--data-dir", str(data), "study", "数据结构", "--no-verify", "--reasoning", "off"],
        out=out2,
        read_line=scripted("继续"),  # type: ignore[arg-type]
        gateway=gw,
    )
    assert "→ 2. 栈和队列" in out2.getvalue()
    assert "思考强度 off" in out2.getvalue()
    fake = gw.providers["fake"]
    assert isinstance(fake, FakeProvider)
    assert {r.reasoning for r in fake.requests if not r.tools} == {"off"}


def test_study_errors(
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "EXAMPREP_OLLAMA_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["--data-dir", str(data), "study", "没有这门课"], out=io.StringIO()) == 2
    assert "找不到课程" in capsys.readouterr().err
    assert cli.main(["--data-dir", str(data), "study", "数据结构"], out=io.StringIO()) == 2
    assert "还没有配置模型" in capsys.readouterr().err
