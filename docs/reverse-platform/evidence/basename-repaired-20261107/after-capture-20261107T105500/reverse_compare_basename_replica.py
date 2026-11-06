#!/usr/bin/env python3
"""Small source replica for the /usr/bin/basename executable comparison fixture."""
import os
import sys

USAGE = "usage: basename string [suffix]\n       basename [-a] [-s suffix] string [...]\n"
ORIGINAL_PROGRAM = "/usr/bin/basename"


def _base(value: str) -> str:
    stripped = value.rstrip("/")
    if not stripped and value.startswith("/"):
        return "/"
    return os.path.basename(stripped)


def main(argv: list[str]) -> int:
    all_args = False
    suffix = None
    args = list(argv)
    while args and args[0].startswith("-") and args[0] != "-":
        option = args.pop(0)
        if option == "--":
            break
        chars = option[1:]
        index = 0
        while index < len(chars):
            flag = chars[index]
            if flag == "a":
                all_args = True
                index += 1
            elif flag == "s":
                if index + 1 < len(chars):
                    suffix = chars[index + 1:]
                elif args:
                    suffix = args.pop(0)
                else:
                    sys.stderr.write(f"{ORIGINAL_PROGRAM}: option requires an argument -- s\n{USAGE}")
                    return 1
                all_args = True
                break
            else:
                sys.stderr.write(f"{ORIGINAL_PROGRAM}: illegal option -- {flag}\n{USAGE}")
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
