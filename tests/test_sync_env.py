import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_env.py"
spec = importlib.util.spec_from_file_location("sync_env", SCRIPT_PATH)
sync_env = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = sync_env
spec.loader.exec_module(sync_env)


def test_merge_env_lines_preserves_values_and_template_shape():
    source = [
        "BOT_A_TOKEN=secret-token\n",
        "PROXY_PORT=9999\n",
        "EXTRA_LOCAL=keep-me\n",
    ]
    template = [
        "# comment stays\n",
        "BOT_A_TOKEN=\n",
        "PROXY_PORT=8787\n",
        "NEW_DEFAULT=abc\n",
    ]

    merged, stats = sync_env.merge_env_lines(source, template)

    assert merged[:4] == [
        "# comment stays\n",
        "BOT_A_TOKEN=secret-token\n",
        "PROXY_PORT=9999\n",
        "NEW_DEFAULT=abc\n",
    ]
    assert "EXTRA_LOCAL=keep-me\n" in merged
    assert stats == {"replaced": 2, "kept_defaults": 1, "extra_preserved": 1}


def test_sync_env_file_updates_source_not_template(tmp_path):
    env_path = tmp_path / ".env"
    template_path = tmp_path / ".env.example"
    env_path.write_text("TOKEN=secret\nOLD_ONLY=1\n", encoding="utf-8")
    template_text = "# template\nTOKEN=\nNEW_VAR=default\n"
    template_path.write_text(template_text, encoding="utf-8")

    stats = sync_env.sync_env_file(env_path, template_path)

    assert env_path.read_text(encoding="utf-8") == (
        "# template\n"
        "TOKEN=secret\n"
        "NEW_VAR=default\n"
        "\n"
        "# Extra variables preserved from the source env because they are not in the template.\n"
        "OLD_ONLY=1\n"
    )
    assert template_path.read_text(encoding="utf-8") == template_text
    assert stats == {"replaced": 1, "kept_defaults": 1, "extra_preserved": 1}
