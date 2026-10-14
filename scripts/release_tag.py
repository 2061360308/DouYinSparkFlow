"""Resolve release metadata as data, without interpolating inputs into shell code."""
import os
from pathlib import Path
import re
import sys


def resolve_tag(event_name, ref_name, input_tag, version):
    tag = ref_name if event_name == "push" else input_tag or f"v{version.strip()}"
    # Keep existing numeric, v-prefixed and release-v-prefixed tags, including
    # prerelease/build suffixes. Reject shell syntax, output-line injection and
    # option-like names before any release job consumes this value.
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", tag)
            or ".." in tag or tag.endswith((".", ".lock"))):
        raise ValueError("发布 tag 只能包含字母、数字、点、下划线、加号和连字符，且必须是有效的 tag 名称")
    return tag


if __name__ == "__main__":
    try:
        tag = resolve_tag(os.getenv("GITHUB_EVENT_NAME", ""), os.getenv("GITHUB_REF_NAME", ""),
                          os.getenv("INPUT_TAG", ""), Path("VERSION").read_text(encoding="utf-8"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(f"tag={tag}")
