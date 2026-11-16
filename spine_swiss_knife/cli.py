"""Command line interface: python -m spine_swiss_knife.cli <tool> [--arg value ...]

Prints the result of the matching ``api`` function as JSON.
Boolean flags: ``--apply`` / ``--no-apply``.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys

from . import api


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spine_swiss_knife.cli",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="tool", required=True)
    for name, fn in api.TOOLS.items():
        sp = sub.add_parser(name.replace("_", "-"), aliases=[name], help=(fn.__doc__ or "").strip().split("\n")[0])
        sp.set_defaults(tool=name)
        for pname, p in inspect.signature(fn).parameters.items():
            flag = "--" + pname.replace("_", "-")
            hint = str(p.annotation)
            required = p.default is inspect.Parameter.empty
            if "bool" in hint:
                sp.add_argument(flag, dest=pname, action=argparse.BooleanOptionalAction,
                                default=p.default)
            elif "int" in hint:
                sp.add_argument(flag, dest=pname, type=int,
                                default=None if required else p.default,
                                required=required)
            elif "dict" in hint:
                sp.add_argument(flag, dest=pname, type=json.loads, default=None)
            else:
                sp.add_argument(flag, dest=pname,
                                default=None if required else p.default,
                                required=required)
    return parser


def main(argv=None) -> int:
    args = vars(_build_parser().parse_args(argv))
    tool = args.pop("tool")
    try:
        result = api.TOOLS[tool](**args)
    except api.SpineError as e:
        print(json.dumps({"error": str(e)}), file=sys.stdout)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
