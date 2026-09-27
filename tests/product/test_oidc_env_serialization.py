from __future__ import annotations

# ruff: noqa: S603, S607
import shlex
import subprocess
from pathlib import Path


def test_pem_env_round_trip(tmp_path: Path) -> None:
    env_file = tmp_path / "integration.env"
    pem = "-----BEGIN PUBLIC KEY-----\nabc\ndef\n-----END PUBLIC KEY-----\n"
    env_file.write_text(
        "RAGOPS_OIDC_ISSUER='http://localhost:8081/realms/ragops-integration'\n"
        "RAGOPS_OIDC_AUDIENCE='ragops-api'\n"
        f"RAGOPS_OIDC_PUBLIC_KEY={shlex.quote(pem)}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", f"source {env_file} && printf '%s' \"$RAGOPS_OIDC_PUBLIC_KEY\""],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == pem


def test_integration_user_profile_is_fully_configured() -> None:
    script = Path(__file__).parents[2] / "scripts/configure_local_oidc_integration.py"
    text = script.read_text(encoding="utf-8")
    for field in (
        '"firstName": "Integration"',
        '"lastName": "User"',
        '"emailVerified": True',
        '"requiredActions": []',
        '"temporary": False',
    ):
        assert field in text
