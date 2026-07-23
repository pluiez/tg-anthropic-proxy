import sys

from starlette.routing import Match

from cc_proxy.main import _parse_args, app, root_head


def test_root_head_probe_matches_local_handler_before_proxy() -> None:
    scope = {
        "type": "http",
        "method": "HEAD",
        "path": "/",
        "root_path": "",
        "headers": [],
    }

    for route in app.router.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            assert route.endpoint is root_head
            return

    raise AssertionError("HEAD / did not match any route")


def test_parse_args_can_disable_claude_code_headers(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["cc_proxy", "--no-claude-code-headers"])

    args = _parse_args()

    assert args.no_claude_code_headers is True
