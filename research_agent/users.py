"""用户管理 CLI（坑 99）：python -m research_agent.users <add|list|disable|enable|rotate>

`add`/`rotate` 只在终端打印一次邀请路径，token 不写任何日志/文件
（库里只有 sha256）。不读写 .env。
"""
import argparse
import re
import secrets
import sys

from . import auth

_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m research_agent.users")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--id", required=True)
    a.add_argument("--name", required=True); a.add_argument("--admin", action="store_true")
    a.add_argument("--standard", type=int, default=2)
    a.add_argument("--fast", type=int, default=6)
    sub.add_parser("list")
    d = sub.add_parser("disable"); d.add_argument("id")
    e = sub.add_parser("enable"); e.add_argument("id")
    r = sub.add_parser("rotate"); r.add_argument("id")
    ns = p.parse_args(argv)

    try:
        if ns.cmd == "add":
            if not _ID_RE.match(ns.id):
                print(f"❌ id 不合法：{ns.id}（要 ^[a-z0-9_-]{{1,32}}$）"); return 1
            token = secrets.token_urlsafe(32)
            limits = None if ns.admin else {"standard": ns.standard, "fast": ns.fast}
            auth.add_user(ns.id, ns.name, token, admin=ns.admin, limits=limits)
            print(f"✅ 已建用户 {ns.id}（{ns.name}{'·admin' if ns.admin else ''}）")
            print(f"邀请路径（只显示这一次）：/api/auth/invite?t={token}")
        elif ns.cmd == "list":
            for u in auth.load_users():
                print(f"{u.id}\t{u.name}\tadmin={u.admin}\t"
                      f"limits={u.limits}\tdisabled={u.disabled}")
        elif ns.cmd == "disable":
            auth.set_disabled(ns.id, True); print(f"✅ 已禁用 {ns.id}")
        elif ns.cmd == "enable":
            auth.set_disabled(ns.id, False); print(f"✅ 已启用 {ns.id}")
        elif ns.cmd == "rotate":
            token = secrets.token_urlsafe(32)
            auth.rotate_token(ns.id, token)
            print(f"✅ 已轮换 {ns.id} 的 token（旧链接立即失效）")
            print(f"新邀请路径（只显示这一次）：/api/auth/invite?t={token}")
        return 0
    except ValueError as e:
        print(f"❌ {e}"); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
