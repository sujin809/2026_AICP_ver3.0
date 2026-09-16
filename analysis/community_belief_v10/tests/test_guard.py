import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard


def test_enforce_removes_api_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("HF_TOKEN", "hf-should-be-removed")
    guard.enforce_no_paid_api()
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN"):
        assert key not in os.environ


def test_enforce_sets_offline_flags(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    guard.enforce_no_paid_api(offline=True)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_openai_package_is_not_installed():
    """유료 SDK가 analysis venv에 들어오면 즉시 실패한다."""
    result = subprocess.run(
        [sys.executable, "-c", "import openai"], capture_output=True
    )
    assert result.returncode != 0, "openai 패키지가 analysis venv에 설치되어 있다"


def test_guard_does_not_load_dotenv(monkeypatch):
    """.env를 읽어 키를 되살리지 않아야 한다."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    guard.enforce_no_paid_api()
    assert "OPENROUTER_API_KEY" not in os.environ
