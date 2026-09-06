# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regression cover for the two code defects found by re-reviewing the SCR
observations OTHER than #6, at the same token-level standard.

Both were reported by Checkmarx under categories that are otherwise entirely
false positives, which is exactly why they were worth isolating: dismissing a
category wholesale is how a real finding inside it gets missed.

  * `TestConfigCoercionNeverLogsValues` — SCR #2 (Filtering Sensitive Logs).
    `coerce_setting` used to log the rejected value with `%r`, and `value` on
    that path can be a DECRYPTED SECRET. Unreachable today only because all
    eight `is_secret` keys happen to be typed `str`; a single non-`str` secret
    key would have made it live.
  * `TestImageHandleIsReleased` — SCR #10 (Improper Resource Shutdown).
    `PIL.Image.open` is lazy and holds its buffer until closed.
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]


class TestConfigCoercionNeverLogsValues:
    """The failure branch of `coerce_setting` must not render the value."""

    def _log_call_args(self) -> list[str]:
        """Every argument expression of the logging call inside coerce_setting."""
        src = (REPO / "backend/app/core/app_config_sync.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "coerce_setting")
        args: list[str] = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"warning", "error", "info", "debug", "exception"}):
                args += [ast.unparse(a) for a in node.args]
                args += [ast.unparse(kw.value) for kw in node.keywords]
        assert args, "expected a logging call in coerce_setting"
        return args

    def test_the_value_is_never_an_argument(self):
        """`value` — which may be a decrypted secret — must not be logged."""
        args = self._log_call_args()
        assert "value" not in args, (
            f"coerce_setting logs the raw config value: {args}. "
            "On the apply_setting_override path this is decrypt_secret() output."
        )

    def test_no_repr_formatting_of_a_value(self):
        """`%r` renders the value even when the name is indirect."""
        args = self._log_call_args()
        fmt = args[0]
        assert "%r" not in fmt, f"format string still uses %r: {fmt!r}"

    def test_the_pydantic_error_object_is_not_logged(self):
        """pydantic embeds the rejected input as `input_value='...'`, so logging
        the exception leaks the secret just as surely as logging `value`."""
        args = self._log_call_args()
        assert "e" not in args, (
            "the ValidationError is logged verbatim; its message contains "
            "input_value=<the secret>. Log type(e).__name__ instead."
        )

    def test_pydantic_really_does_echo_the_input(self):
        """Guards the premise above — if pydantic ever stops echoing the input,
        this test tells us the reasoning changed rather than silently passing."""
        pydantic = pytest.importorskip("pydantic")
        adapter = pydantic.TypeAdapter(int)
        with pytest.raises(Exception) as excinfo:
            adapter.validate_python("sk-live-secret-value")
        assert "sk-live-secret-value" in str(excinfo.value), (
            "premise broken: pydantic no longer echoes the rejected input"
        )

    def test_the_key_and_type_are_still_logged(self):
        """The other direction: a scrub that removes all diagnostics gets
        reverted. The field name and target type carry no secret."""
        args = self._log_call_args()
        joined = " ".join(args)
        assert "key" in joined, "the config key must still be logged"
        assert "annotation" in joined or "type" in joined, (
            "the failing target type must still be logged"
        )

    def test_all_secret_keys_are_currently_str(self):
        """Documents WHY this was latent rather than live. If someone adds a
        non-`str` secret key, this test fails and points at the reason."""
        src = (REPO / "backend/app/api/app_config.py").read_text(encoding="utf-8")
        schema = next(
            ast.literal_eval(n.value) for n in ast.parse(src).body
            if isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") == "CONFIG_SCHEMA"
        )
        secret_keys = [r["key"] for r in schema if r.get("is_secret")]
        assert secret_keys, "expected some is_secret keys"

        cfg = (REPO / "backend/app/core/config.py").read_text(encoding="utf-8")
        tree = ast.parse(cfg)
        annotations: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                annotations[node.target.id] = ast.unparse(node.annotation)
        non_str = {k: annotations.get(k) for k in secret_keys
                   if annotations.get(k) not in ("str", None)}
        assert not non_str, (
            f"secret keys are no longer all `str`: {non_str}. That is allowed, "
            "but it means coerce_setting's failure branch is now REACHABLE with "
            "a real secret — keep the value out of the log message."
        )


class TestImageHandleIsReleased:
    """`PIL.Image.open` must be scoped so the buffer is released."""

    def test_diagram_image_is_opened_in_a_with_block(self):
        src = (REPO / "backend/app/docgen/deck/templates/_layout_builders.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        opens = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "open"
                 and "PILImage" in ast.unparse(n.func)]
        assert opens, "expected a PILImage.open call"

        managed = {id(item.context_expr) for n in ast.walk(tree)
                   if isinstance(n, ast.With) for item in n.items}
        unmanaged = [ast.unparse(o) for o in opens if id(o) not in managed]
        assert not unmanaged, (
            f"PILImage.open outside a with-block: {unmanaged}. The image object "
            "holds its buffer until closed."
        )

    def test_size_is_still_read_correctly(self):
        """Behavioural guard: closing the image must not break the aspect math."""
        PILImage = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        PILImage.new("RGB", (320, 200), "red").save(buf, format="PNG")
        with PILImage.open(io.BytesIO(buf.getvalue())) as img:
            width, height = img.size
        assert (width, height) == (320, 200)
        assert width / max(height, 1) == pytest.approx(1.6)
