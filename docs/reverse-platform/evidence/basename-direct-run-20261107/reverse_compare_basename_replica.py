#!/usr/bin/env python3
"""Small source replica for the /usr/bin/basename executable comparison fixture."""
import os
import sys

USAGE = "usage: basename string [suffix]\n       basename [-a] [-s suffix] string [...]\n"


def _base(value: str) -> str:
    if value == "/":
        return "/"
    result = os.path.basename(value.rstrip("/"))
    return result


def main(argv: list[str]) -> int:
    all_args = False
    suffix = None
    args = list(argv)
    while args and args[0].startswith("-") and args[0] != "-":
        option = args.pop(0)
        if option == "--":
            break
        if option == "-a":
            all_args = True
        elif option == "-s" and args:
            suffix = args.pop(0)
            all_args = True
        else:
            bad = option[1:2] or "-"
            sys.stderr.write(f"basename: illegal option -- {bad}\n{USAGE}")
            return 1
    if not args or (not all_args and len(args) > 2):
        sys.stderr.write(USAGE)
        return 1
    if not all_args and len(args) == 2:
        suffix = args.pop()
    for value in args:
        name = _base(value)
        if suffix and name.endswith(suffix) and name != suffix:
            name = name[:-len(suffix)]
        sys.stdout.write(name + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
